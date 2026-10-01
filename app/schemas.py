"""Pydantic 请求/响应模型。"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


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


# ------------------------------------------------------------ 规则修订

class RuleValuesOut(BaseModel):
    """规则四项值（与创建窗口请求体同义）。"""

    allowed_temp_min: float
    allowed_temp_max: float
    max_continuous_exceed_seconds: float
    max_sample_interval_seconds: float


class RuleRevisionSubmit(BaseModel):
    """馆员修订审定窗口规则：窗口号 + 目标规则修订号 + 四项完整新值 + 非空原因。

    窗口初始规则视为第 1 版：
    - revision 等于“当前版 + 1”才生效；修订成功后窗口规则立即更新并按当前
      活动读数重算，后续签发采用新规则，既有证书快照不变；
    - revision 等于当前版且四项规则值与原因均与已存修订一致 -> 幂等重放
      （不产生新记录）；同号但任一值或原因不一致 -> 409；
    - revision 小于当前版（旧号）或大于“当前版 + 1”（跳号）-> 409；
    - 四项数值必须有限、温度下限不大于上限、两个时长为正（gt=0）。
    """

    revision: int = Field(..., ge=1, description="目标规则修订号（须等于当前版 + 1；重放则等于当前版）")
    allowed_temp_min: float = Field(..., description="新的允许温度下限（含），°C")
    allowed_temp_max: float = Field(..., description="新的允许温度上限（含），°C")
    max_continuous_exceed_seconds: float = Field(..., gt=0, description="新的最长连续超限时长，秒（正）")
    max_sample_interval_seconds: float = Field(..., gt=0, description="新的最大采样间隔，秒（正）")
    reason: str = Field(..., min_length=1, max_length=500,
                        description="修订原因（非空，不能只含空白字符）")

    @field_validator("allowed_temp_min", "allowed_temp_max")
    @classmethod
    def _temp_finite(cls, v: float) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("温度必须为有限数值")
        return v

    @field_validator("max_continuous_exceed_seconds", "max_sample_interval_seconds")
    @classmethod
    def _duration_finite_positive(cls, v: float) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("时长必须为有限数值")
        if v <= 0:
            raise ValueError("时长必须为正数")
        return v

    @field_validator("reason")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("修订原因不能为空（不能只含空白字符）")
        return v


class RuleRevisionOut(BaseModel):
    """一条规则修订记录（第 1 版为窗口初始规则，before 四项为 null）。"""

    rule_revision: int
    before: Optional[RuleValuesOut] = Field(
        None, description="修订前四项规则值；第 1 版（初始规则）为 null")
    after: RuleValuesOut = Field(..., description="修订后（该版生效）四项规则值")
    reason: str
    revised_at: str = Field(..., description="修订操作时间（UTC）；第 1 版为窗口创建时间")


class RuleRevisionResponse(BaseModel):
    effect: str = Field(..., description="revised | idempotent")
    rule_revision: RuleRevisionOut
    current_evaluation: EvaluationOut = Field(
        ..., description="按新规则与当前活动读数立即重算的审定结果")


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
    rule_revision: int = Field(1, description="当前规则修订号（初始规则为第 1 版）")
    created_at: str
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


class ReportRulesChange(BaseModel):
    field: str
    label_zh: str
    before: float
    after: float
    delta: float
    unit_zh: str


class ReportRulesDiff(BaseModel):
    """跨规则版本比对：两证快照各自规则修订号与四项规则的前后值。"""

    rule_revision_before: int
    rule_revision_after: int
    rules_changed: bool
    changed_fields: list[str] = Field(
        ..., description="allowed_temp_min | allowed_temp_max | "
                        "max_continuous_exceed_seconds | max_sample_interval_seconds 的子集")
    before: dict[str, Any] = Field(..., description="旧证快照的四项规则值与规则修订号")
    after: dict[str, Any] = Field(..., description="新证快照的四项规则值与规则修订号")
    changes: list[ReportRulesChange]


class ReportCertificateRef(BaseModel):
    id: int
    window_id: int
    version: int
    verdict: str
    verdict_label_zh: str
    issued_at: str
    note: Optional[str]


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
