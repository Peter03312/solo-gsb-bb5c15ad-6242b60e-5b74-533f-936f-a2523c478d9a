"""FastAPI 入口：博物馆展柜温度暴露审定服务。"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, Query, status

from . import services
from .database import connect, get_conn, init_db, write_transaction
from .models import VERDICT_LABELS_ZH, Verdict
from .schemas import (
    CertificateOut,
    EvaluationOut,
    IssueRequest,
    ReadingBatchSubmit,
    ReadingBatchSubmitResponse,
    ReadingOut,
    ReadingRevisionOut,
    ReadingSubmit,
    ReadingSubmitResponse,
    ReadingVoidResponse,
    ReadingVoidSubmit,
    RevisionReportOut,
    RuleRevisionOut,
    RuleRevisionResponse,
    RuleRevisionSubmit,
    WindowCreate,
    WindowOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时幂等建表并初始化 WAL
    conn = connect()
    try:
        init_db(conn)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="博物馆展柜温度暴露审定服务",
    version="1.4.0",
    description=(
        "对展柜在给定审定窗口内的温度读数进行暴露审定："
        "输出正常/超限/未知区段与最长连续超限时长，"
        "支持读数修订、幂等提交（单条与批量补传）、误采作废与恢复、"
        "窗口规则版本化修订（初始规则为第 1 版，连续修订号 + 非空原因）、"
        "证书版本化签发，"
        "并可对同一窗口两张已签发证书的不可变快照生成修订影响报告"
        "（跨规则版本时分别依两证快照重建时间线并给出四项规则前后值）。"
    ),
    lifespan=lifespan,
)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def root() -> dict[str, Any]:
    return {
        "service": "museum-case-temperature-audit",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "verdicts": {v.value: VERDICT_LABELS_ZH[v.value] for v in Verdict},
    }


# ---------------------------------------------------------------- 窗口

@app.post("/api/v1/windows", response_model=WindowOut, status_code=status.HTTP_201_CREATED,
          tags=["windows"])
def create_window(
    payload: WindowCreate, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    with write_transaction(conn):
        return services.create_window(conn, payload)


@app.get("/api/v1/windows", response_model=list[WindowOut], tags=["windows"])
def list_windows(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict[str, Any]]:
    return services.list_windows(conn, limit=limit, offset=offset)


@app.get("/api/v1/windows/{window_id}", response_model=WindowOut, tags=["windows"])
def get_window(
    window_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    row = services.get_window_row(conn, window_id)
    return services.window_detail(conn, row)


@app.get("/api/v1/windows/{window_id}/evaluation", response_model=EvaluationOut,
         tags=["windows"])
def get_evaluation(
    window_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    row = services.get_window_row(conn, window_id)
    return services.compute_evaluation(conn, row)


# ------------------------------------------------------------ 窗口规则修订

@app.post(
    "/api/v1/windows/{window_id}/rule-revisions",
    response_model=RuleRevisionResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["windows"],
)
def revise_window_rules(
    window_id: int,
    payload: RuleRevisionSubmit,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """馆员修订审定窗口规则（温度上下限、最长连续超限时长、最大采样间隔）。

    提交窗口标识、目标规则修订号、四项完整新值与非空原因：
    - 窗口初始规则为第 1 版；目标号为当前版 + 1 才生效（effect=revised），
      窗口起止时间与读数保持不变，成功后留存前后规则/原因/时间，并按当前
      活动读数在同一事务内立即重算；后续签发采用新规则，既有证书快照不变；
    - 目标号等于当前版且四项值与原因均一致 -> 幂等重放（effect=idempotent，
      不留新记录）；同号任一不一致 -> 409；
    - 旧号（小于当前版）或跳号（大于当前版 + 1）-> 409，不留新记录；
    - 数值非有限、温度下限大于上限、时长非正、原因空白 -> 422；
    - 窗口不存在 -> 404。
    """
    with write_transaction(conn):
        effect, revision = services.revise_window_rules(conn, window_id, payload)
        win = services.get_window_row(conn, window_id)
        evaluation = services.compute_evaluation(conn, win)
        return {"effect": effect, "rule_revision": revision,
                "current_evaluation": evaluation}


@app.get(
    "/api/v1/windows/{window_id}/rule-revisions",
    response_model=list[RuleRevisionOut],
    tags=["windows"],
)
def list_window_rule_revisions(
    window_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> list[dict[str, Any]]:
    """窗口全部规则修订历史（第 1 版为初始规则），按修订号升序。"""
    services.get_window_row(conn, window_id)
    return services.list_rule_revisions(conn, window_id)


# ---------------------------------------------------------------- 读数

@app.get("/api/v1/windows/{window_id}/readings", response_model=list[ReadingOut],
         tags=["readings"])
def list_readings(
    window_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> list[dict[str, Any]]:
    services.get_window_row(conn, window_id)
    return [services.reading_to_dict(r) for r in services.list_active_readings(conn, window_id)]


@app.post(
    "/api/v1/windows/{window_id}/readings",
    response_model=ReadingSubmitResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["readings"],
)
def submit_reading(
    window_id: int,
    payload: ReadingSubmit,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    # 单个写事务：校验 + 替换/插入 + 基于同一状态立即重算
    with write_transaction(conn):
        win = services.get_window_row(conn, window_id)
        effect, reading = services.submit_reading(conn, window_id, payload)
        evaluation = services.compute_evaluation(conn, win)
        return {"effect": effect, "reading": reading, "current_evaluation": evaluation}


@app.post(
    "/api/v1/windows/{window_id}/readings/batch",
    response_model=ReadingBatchSubmitResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["readings"],
)
def submit_reading_batch(
    window_id: int,
    payload: ReadingBatchSubmit,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """采样仪器补传：在一个写事务内整批提交非空读数批次。

    - 每项沿用单条提交的 reading_id/revision/measured_at/temperature 语义，
      以及时区、窗口闭区间边界、幂等/修订/冲突规则；同批 reading_id 不得重复。
    - 同刻唯一按批次应用后的**最终状态**校验：允许两条读数都以更高修订号
      互换采样时刻，不按写入顺序误拒。
    - 任一项冲突（409）或无效（422）整批拒收，活动读数与修订历史均不改变。
    - 成功时一次提交全部变化，逐项返回 created/updated/idempotent 及读数，
      并返回按最终状态一次性重算的审定结果；同批重放不产生新修订。
    """
    # 单个写事务：整批校验 + 整批落库 + 基于最终状态一次重算
    with write_transaction(conn):
        win = services.get_window_row(conn, window_id)
        results = services.submit_reading_batch(conn, window_id, payload)
        evaluation = services.compute_evaluation(conn, win)
        return {
            "results": [{"effect": effect, "reading": reading}
                        for effect, reading in results],
            "current_evaluation": evaluation,
        }


@app.post(
    "/api/v1/windows/{window_id}/readings/{reading_id}/void",
    response_model=ReadingVoidResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["readings"],
)
def void_reading(
    window_id: int,
    reading_id: str,
    payload: ReadingVoidSubmit,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """作废一条误采读数：凭递增修订号 + 非空原因。

    - 作废作为该 ID 的一条修订留存（原因、操作时间、前一版本），
      当前审定立即剔除该读数并在同一事务内重算；
    - 相同作废修订号 + 相同原因重放 -> 幂等（effect=idempotent）；
      相同修订号原因不同、或修订号不高于当前 -> 409；
    - 读数从未存在 -> 404；已作废的读数不能再次以新修订号作废 -> 409；
    - 恢复：单条或批量提交更高修订号的读数（effect=restored）。
    """
    # 单个写事务：校验 + 作废落库 + 基于同一状态立即重算
    with write_transaction(conn):
        win = services.get_window_row(conn, window_id)
        effect, record = services.void_reading(conn, window_id, reading_id, payload)
        evaluation = services.compute_evaluation(conn, win)
        return {"effect": effect, "void": record, "current_evaluation": evaluation}


@app.get(
    "/api/v1/windows/{window_id}/readings/{reading_id}/revisions",
    response_model=list[ReadingRevisionOut],
    tags=["readings"],
)
def list_reading_revisions(
    window_id: int,
    reading_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict[str, Any]]:
    """该读数 ID 的全部修订历史（含作废记录），按修订号升序。"""
    services.get_window_row(conn, window_id)
    return services.list_reading_revisions(conn, window_id, reading_id)


# ---------------------------------------------------------------- 签发

@app.post(
    "/api/v1/windows/{window_id}/certificates/issue",
    response_model=CertificateOut,
    status_code=status.HTTP_201_CREATED,
    tags=["certificates"],
)
def issue_certificate(
    window_id: int,
    payload: Optional[IssueRequest] = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    note = payload.note if payload is not None else None
    # BEGIN IMMEDIATE：与读数修订并发时，要么修订已提交（纳入本次签发），
    # 要么修订等待锁（在签发提交后生效，不改变本次快照）。
    with write_transaction(conn):
        return services.issue_certificate(conn, window_id, note)


@app.get("/api/v1/windows/{window_id}/certificates", response_model=list[CertificateOut],
         tags=["certificates"])
def list_certificates(
    window_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> list[dict[str, Any]]:
    return services.list_certificates(conn, window_id)


# 注意：本静态路径必须注册在 /api/v1/certificates/{certificate_id} 之前，
# 否则 "revision-report" 会先被 {certificate_id:int} 匹配并触发 422。
@app.get("/api/v1/certificates/revision-report",
         response_model=RevisionReportOut, tags=["certificates"])
def get_revision_report(
    old_certificate_id: int = Query(..., ge=1, description="旧版（较早签发）证书主键 ID"),
    new_certificate_id: int = Query(..., ge=1, description="新版（较晚签发）证书主键 ID"),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """对同一审定窗口两张已签发证书的不可变快照生成修订影响报告。

    只依据所选快照比较，不读取当前活动读数：
    - 读数差异（新增/变更读数，比较 revision、采样时间、温度）；
    - 两版审定时间线按全部区段边界对齐后，状态发生变化的半开区间，
      含变化前后状态、各侧读数 ID/修订号，未知侧区分
      “采样间隔超限（sample_gap）”与“边界空白（boundary_gap）”；
    - 汇总结论与最长连续超限时长的变化。
    窗口不一致、版本未按先后排列（旧版版本号须严格小于新版）、
    两 ID 相同均返回 422；证书不存在返回 404。
    """
    return services.revision_report(conn, old_certificate_id, new_certificate_id)


@app.get("/api/v1/certificates/{certificate_id}", response_model=CertificateOut,
         tags=["certificates"])
def get_certificate(
    certificate_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    return services.get_certificate(conn, certificate_id)
