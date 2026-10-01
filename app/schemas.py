"""Pydantic 请求/响应模型。"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


def _require_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("时间戳必须带时区信息，例如 2026-10-01T00:00:00Z 或 +08:00")
    return dt


class WindowCreate(BaseModel):
    """创建审定窗口。

    - allowed_temp_min/max：允许温度区间（闭区间，单位 °C）
    - max_continuous_exceed_seconds：最长连续超限时长（秒，超过它判不合格）
    - max_sample_interval_seconds：最大采样间隔（秒，超过则相邻读数间记为未知）
    """

    start_at: datetime = Field(..., description="窗口起始时间（含），带时区")
    end_at: datetime = Field(..., description="窗口结束时间（读数提交含该时刻；输出区段为半开），带时区")
    allowed_temp_min: float = Field(..., description="允许温度下限（含），°C")
    allowed_temp_max: float = Field(..., description="允许温度上限（含），°C")
    max_continuous_exceed_seconds: float = Field(..., gt=0, description="最长连续超限时长，秒")
    max_sample_interval_seconds: float = Field(..., gt=0, description="最大采样间隔，秒")
    name: Optional[str] = Field(None, max_length=200, description="可选备注名，如展柜编号")

    @field_validator("start_at", "end_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        return _require_aware(v)

    @field_validator("allowed_temp_max")
    @classmethod
    def _temp_range(cls, v: float, info: Any) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("温度必须为有限数值")
        return v

    @field_validator("allowed_temp_min")
    @classmethod
    def _temp_min_finite(cls, v: float) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("温度必须为有限数值")
        return v


class ReadingSubmit(BaseModel):
    """提交（或修订）一条温度读数。

    幂等与修订规则（按 同一窗口 + reading_id）：
    - revision 已存在：内容（时间戳/温度）完全一致 -> 幂等返回；
      内容不一致 -> 409 冲突。
    - revision 高于当前 -> 替换当前读数并重算；
    - revision 低于当前 -> 409 拒绝（旧修订号不得覆盖新修订号）。
    """

    reading_id: str = Field(..., min_length=1, max_length=100, description="读数 ID（窗口内唯一）")
    revision: int = Field(..., ge=1, description="修订号，从 1 开始递增")
    measured_at: datetime = Field(..., description="读数时间戳，带时区")
    temperature: float = Field(..., description="温度，°C")

    @field_validator("measured_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        return _require_aware(v)

    @field_validator("temperature")
    @classmethod
    def _finite(cls, v: float) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("温度必须为有限数值")
        return v


class ReadingBatchSubmit(BaseModel):
    """采样仪器补传：一个审定窗口下的一批非空读数。

    批次在单个写事务内整体校验、整体提交：任一项冲突或无效即整批拒收
    （活动读数与修订历史均不变）。每项沿用单条提交的 reading_id / revision /
    measured_at / temperature 语义与时区、窗口边界、修订规则；同批不得
    重复 reading_id；同刻唯一按“批次应用后的最终状态”校验，允许两条读数
    以更高修订号互换采样时刻。对已作废的 reading_id，批内以更高修订号
    提交即恢复该读数（effect=restored）。
    """

    readings: list[ReadingSubmit] = Field(
        ..., min_length=1, description="非空读数批次（至少 1 项）；同批 reading_id 不得重复"
    )


class ReadingVoidSubmit(BaseModel):
    """作废一条误采读数：凭递增修订号 + 非空原因。

    - revision 必须高于该读数当前修订号；作废本身作为该 ID 的一条修订
      留存（原因、操作时间、前一版本），当前审定立即剔除该读数并重算。
    - 相同作废修订号 + 相同原因重放 -> 幂等；相同修订号原因不同、
      或修订号不高于当前 -> 409；读数从未存在 -> 404；
      已作废的读数不能再次以新修订号作废 -> 409。
    """

    revision: int = Field(..., ge=1, description="作废修订号，必须高于该读数当前修订号")
    reason: str = Field(..., min_length=1, max_length=500,
                        description="作废原因（非空，不能只含空白字符）")

    @field_validator("reason")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("作废原因不能为空（不能只含空白字符）")
        return v


class IssueRequest(BaseModel):
    note: Optional[str] = Field(None, max_length=500, description="签发备注")


def _finite_number(v: float) -> float:
    """四项数值参数的通用有限性校验：拒绝 NaN / ±Infinity。"""
    if math.isnan(v) or math.isinf(v):
        raise ValueError("数值必须为有限数（不接受 NaN 或 Infinity）")
    return v


class WindowRuleRevisionSubmit(BaseModel):
    """馆员修订一个审定窗口的四项规则。

    四项完整新值（温度上下限、最长连续超限时长、最大采样间隔）与非空原因
    必须随请求一次性提交；窗口初始规则视为第 1 版：

    - target_revision == 当前版 + 1：生效，追加新版本并按当前活动读数立即重算；
    - target_revision == 当前版，且四项值与原因均与当前版一致：重放幂等，
      不新增记录；
    - target_revision == 当前版但值或原因不一致：409 同号冲突；
    - 目标号低于当前版（旧号）或高于当前版 + 1（跳号）：409 拒绝；
    - 四项数值须有限、温度下限不大于上限、两个时长为正：422；
      原因非空（不能只含空白）。
    窗口起止时间与读数不随规则修订改变。
    """

    target_revision: int = Field(..., ge=1, description="目标规则修订号；仅当等于当前版 + 1 时生效")
    allowed_temp_min: float = Field(..., description="新允许温度下限（含），°C")
    allowed_temp_max: float = Field(..., description="新允许温度上限（含），°C")
    max_continuous_exceed_seconds: float = Field(..., description="新最长连续超限时长，秒（正）")
    max_sample_interval_seconds: float = Field(..., description="新最大采样间隔，秒（正）")
    reason: str = Field(..., min_length=1, max_length=500,
                        description="修订原因（非空，不能只含空白字符）")

    @field_validator(
        "allowed_temp_min", "allowed_temp_max",
        "max_continuous_exceed_seconds", "max_sample_interval_seconds",
    )
    @classmethod
    def _all_finite(cls, v: float) -> float:
        return _finite_number(v)

    @field_validator("reason")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("修订原因不能为空（不能只含空白字符）")
        return v

    @model_validator(mode="after")
    def _check_rule_values(self) -> "WindowRuleRevisionSubmit":
        if self.allowed_temp_max < self.allowed_temp_min:
            raise ValueError("allowed_temp_max 不能小于 allowed_temp_min（温度下限不大于上限）")
        if self.max_continuous_exceed_seconds <= 0:
            raise ValueError("max_continuous_exceed_seconds 必须为正数")
        if self.max_sample_interval_seconds <= 0:
            raise ValueError("max_sample_interval_seconds 必须为正数")
        return self


class SegmentOut(BaseModel):
    start: str
    end: str
    status: str
    duration_seconds: float


class EvaluationOut(BaseModel):
    verdict: str
    normal_segments: list[SegmentOut]
    exceeded_segments: list[SegmentOut]
    unknown_segments: list[SegmentOut]
    max_continuous_exceeded_seconds: float
    max_exceed_seconds: float
    has_exceeded: bool
    has_unknown: bool
    reading_count: int


class ReadingOut(BaseModel):
    reading_id: str
    revision: int
    measured_at: str
    temperature: float
    status: str
    created_at: str


class WindowOut(BaseModel):
    id: int
    name: Optional[str]
    start_at: str
    end_at: str
    allowed_temp_min: float
    allowed_temp_max: float
    max_continuous_exceed_seconds: float
    max_sample_interval_seconds: float
    created_at: str
    rule_revision: Optional[int] = Field(
        None, description="当前审定规则版本号（初始规则为第 1 版）"
    )
    current_evaluation: Optional[EvaluationOut] = None
    active_readings: list[ReadingOut] = []
    latest_certificate_version: Optional[int] = None
    latest_verdict: Optional[str] = None


class ReadingSubmitResponse(BaseModel):
    effect: str = Field(..., description="created | updated | restored | idempotent")
    reading: ReadingOut
    current_evaluation: EvaluationOut


class ReadingBatchItemResult(BaseModel):
    effect: str = Field(..., description="created | updated | restored | idempotent")
    reading: ReadingOut


class ReadingBatchSubmitResponse(BaseModel):
    results: list[ReadingBatchItemResult] = Field(
        ..., description="逐项结果，顺序与请求一致；每项给出 created/updated/restored/idempotent 及最终读数"
    )
    current_evaluation: EvaluationOut = Field(
        ..., description="按批次最终状态一次性重算的审定结果"
    )


# ------------------------------------------------------------ 误采作废

class VoidedPreviousOut(BaseModel):
    """被作废的前一版本读数内容。"""

    revision: int
    measured_at: str
    temperature: float


class VoidRecordOut(BaseModel):
    """作废修订记录：作废作为该 ID 的一条修订留存。"""

    reading_id: str
    revision: int = Field(..., description="作废修订号")
    status: str = Field(..., description="固定为 voided")
    reason: str = Field(..., description="作废原因")
    voided_at: str = Field(..., description="作废操作时间（UTC）")
    previous: VoidedPreviousOut = Field(..., description="被作废的前一版本读数")


class ReadingVoidResponse(BaseModel):
    effect: str = Field(..., description="voided | idempotent")
    void: VoidRecordOut
    current_evaluation: EvaluationOut = Field(
        ..., description="剔除该读数后立即重算的审定结果"
    )


class ReadingRevisionOut(BaseModel):
    """读数修订历史行（含作废记录）。"""

    revision: int
    status: str = Field(..., description="active | superseded | voided")
    measured_at: str
    temperature: float
    created_at: str
    void_reason: Optional[str] = Field(None, description="作废原因（仅 voided 行）")
    voided_at: Optional[str] = Field(None, description="作废操作时间（仅 voided 行）")
    prev_revision: Optional[int] = Field(None, description="被作废的前一版本修订号（仅 voided 行）")


# ------------------------------------------------------------ 窗口规则修订

class RuleValuesOut(BaseModel):
    """一版审定规则的四项完整值（不含窗口起止时间）。"""

    allowed_temp_min: float
    allowed_temp_max: float
    max_continuous_exceed_seconds: float
    max_sample_interval_seconds: float


class RuleRevisionOut(RuleValuesOut):
    """规则修订历史行：四项值 + 版本号 + 原因 + 操作时间。"""

    revision: int = Field(..., description="规则版本号（初始规则为第 1 版）")
    reason: str = Field(..., description="本版修订原因（第 1 版为系统播种说明）")
    created_at: str = Field(..., description="本版生效时间（UTC）")


class WindowRuleRevisionResponse(BaseModel):
    effect: str = Field(..., description="revised | idempotent")
    window_id: int
    current_rule_revision: int = Field(..., description="修订后（或幂等重放所对应）的当前规则版本号")
    before: RuleValuesOut = Field(..., description="修订前一版规则的四项值")
    after: RuleRevisionOut = Field(..., description="本次生效（或重放）版本的四项值、原因与时间")
    current_evaluation: EvaluationOut = Field(
        ..., description="按当前活动读数与新规则立即重算的审定结果"
    )


class CertificateOut(BaseModel):
    id: int
    window_id: int
    version: int
    verdict: str
    verdict_label_zh: str
    issued_at: str
    note: Optional[str]
    snapshot: dict[str, Any]


# ------------------------------------------------------------ 修订影响报告

class ReportReading(BaseModel):
    reading_id: str
    revision: int
    measured_at: str
    temperature: float


class ReportReadingChange(BaseModel):
    reading_id: str
    before: ReportReading
    after: ReportReading
    changed_fields: list[str] = Field(..., description="revision | measured_at | temperature")


class ReportReadingsDiff(BaseModel):
    added: list[ReportReading]
    removed: list[ReportReading]
    changed: list[ReportReadingChange]
    added_count: int
    removed_count: int
    changed_count: int


class ReportTimelineSide(BaseModel):
    status: str
    # 键随状态而定：已知区段 {"reading_id","revision"}；
    # sample_gap 另含 unknown_reason 与 left/right 读数 ID/修订号；
    # boundary_gap 另含 boundary_side=leading|trailing|empty。
    evidence: dict[str, Any]


class ReportTimelineChange(BaseModel):
    start: str
    end: str
    duration_seconds: float
    before: ReportTimelineSide
    after: ReportTimelineSide


class ReportTimeline(BaseModel):
    max_sample_interval_seconds: float
    changes: list[ReportTimelineChange]
    change_count: int


class ReportCertificateRef(BaseModel):
    id: int
    window_id: int
    version: int
    verdict: str
    verdict_label_zh: str
    issued_at: str
    note: Optional[str]


class ReportRuleSide(RuleValuesOut):
    """报告中一版证书快照使用的规则版本与四项值。"""

    rule_revision: int


class ReportRulesDiff(BaseModel):
    """跨规则版本的四项规则前后值与变化（即使结论未变也能看出规则变更）。"""

    before: ReportRuleSide
    after: ReportRuleSide
    changed_fields: list[str] = Field(
        ..., description="发生变化的四项规则字段子集（rule_revision 单独以 revision_changed 表示）"
    )
    revision_changed: bool = Field(..., description="两证快照的规则版本号是否不同")
    changed: bool = Field(..., description="四项规则值是否至少一项发生变化")


class ReportSummary(BaseModel):
    verdict_before: str
    verdict_after: str
    verdict_before_label_zh: str
    verdict_after_label_zh: str
    verdict_changed: bool
    verdict_direction: str = Field(..., description="改善 | 恶化 | 持平")
    max_continuous_exceeded_seconds_before: float
    max_continuous_exceeded_seconds_after: float
    max_continuous_exceeded_seconds_delta: float
    max_exceed_seconds_limit: float
    timeline_change_count: int
    conclusion_zh: str


class RevisionReportOut(BaseModel):
    window: dict[str, Any]
    old_certificate: ReportCertificateRef
    new_certificate: ReportCertificateRef
    rules_diff: ReportRulesDiff
    readings_diff: ReportReadingsDiff
    timeline: ReportTimeline
    summary: ReportSummary


class ErrorBody(BaseModel):
    detail: str
