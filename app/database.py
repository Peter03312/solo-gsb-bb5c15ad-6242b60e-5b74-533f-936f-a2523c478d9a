"""SQLite 连接管理与建表。

并发策略：
- 开启 WAL，允许签发（读事务）与修订（写事务）并发；
- 写操作使用 BEGIN IMMEDIATE 立即拿写锁，busy_timeout 期间等待，
  避免升级死锁；
- 签发在一个事务内读取“同一已提交状态”的全部活动读数并生成快照，
  事务期间写入方要么在签发前提交（修订可见，签发与修订并发时以
  同一已提交状态计算），要么在签发提交后才生效（快照不受影响）。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import Request

from .config import settings

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    start_at TEXT NOT NULL,                 -- ISO-8601 UTC
    end_at TEXT NOT NULL,
    allowed_temp_min REAL NOT NULL,
    allowed_temp_max REAL NOT NULL,
    max_continuous_exceed_seconds REAL NOT NULL,
    max_sample_interval_seconds REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 每个（窗口, 读数 ID）只有一行“当前活动”读数
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER NOT NULL REFERENCES windows(id),
    reading_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    measured_at TEXT NOT NULL,
    temperature REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(window_id, reading_id),
    -- 同一窗口内不同读数不允许同一时刻
    UNIQUE(window_id, measured_at)
);

CREATE INDEX IF NOT EXISTS idx_readings_window_time
    ON readings(window_id, measured_at);

-- 每次提交（含同修订号幂等重放）都留痕，用于内容冲突检测；
-- 误采作废也作为该 ID 的一条修订（status='voided'）留存原因、操作时间与前一版本
CREATE TABLE IF NOT EXISTS reading_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER NOT NULL REFERENCES windows(id),
    reading_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    measured_at TEXT NOT NULL,
    temperature REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',  -- active | superseded | voided
    void_reason TEXT,                       -- 作废原因（仅 voided 行）
    voided_at TEXT,                         -- 作废操作时间（仅 voided 行）
    prev_revision INTEGER,                  -- 被作废的前一版本修订号（仅 voided 行）
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(window_id, reading_id, revision)
);

-- 证书版本：同一窗口每次签发新增一行，快照不可变
CREATE TABLE IF NOT EXISTS certificates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER NOT NULL REFERENCES windows(id),
    version INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    note TEXT,
    issued_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(window_id, version)
);

CREATE INDEX IF NOT EXISTS idx_certificates_window
    ON certificates(window_id, version);

-- 窗口审定规则版本：窗口创建即播种第 1 版（初始规则），馆员修订严格按
-- “当前版 + 1”追加；windows 表的四项参数始终等于最新版规则值。
-- 每次修订留存四项规则前后值（前值可由上一行得到）、原因与操作时间。
CREATE TABLE IF NOT EXISTS rule_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER NOT NULL REFERENCES windows(id),
    revision INTEGER NOT NULL,                 -- 初始规则为 1，逐次 +1
    allowed_temp_min REAL NOT NULL,
    allowed_temp_max REAL NOT NULL,
    max_continuous_exceed_seconds REAL NOT NULL,
    max_sample_interval_seconds REAL NOT NULL,
    reason TEXT NOT NULL,                      -- 修订原因（非空；第 1 版为系统播种说明）
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(window_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_rule_revisions_window
    ON rule_revisions(window_id, revision);
"""

# 旧库迁移：CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，
# 这里按 PRAGMA table_info 检查后逐列 ALTER（幂等）。
_COLUMN_MIGRATIONS = (
    ("reading_revisions", "void_reason",
     "ALTER TABLE reading_revisions ADD COLUMN void_reason TEXT"),
    ("reading_revisions", "voided_at",
     "ALTER TABLE reading_revisions ADD COLUMN voided_at TEXT"),
    ("reading_revisions", "prev_revision",
     "ALTER TABLE reading_revisions ADD COLUMN prev_revision INTEGER"),
)


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or settings.db_path
    conn = sqlite3.connect(
        path,
        timeout=settings.db_timeout,
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={settings.busy_timeout_ms};")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


# 初始规则版本的系统播种原因（窗口创建即第 1 版）
INITIAL_RULE_REASON = "窗口创建时的初始审定规则（第 1 版）"


def init_db(conn: sqlite3.Connection) -> None:
    """幂等建表；对已存在的旧库补齐作废相关列。

    同时为本功能上线前已存在、尚无规则版本的窗口补播第 1 版规则
    （取 windows 表当前四项参数），保证“窗口初始规则视为第 1 版”不变式
    对旧库同样成立。
    """
    conn.executescript(SCHEMA_SQL)
    for table, column, ddl in _COLUMN_MIGRATIONS:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)
    conn.execute(
        """
        INSERT INTO rule_revisions (window_id, revision, allowed_temp_min,
                                    allowed_temp_max, max_continuous_exceed_seconds,
                                    max_sample_interval_seconds, reason)
        SELECT w.id, 1, w.allowed_temp_min, w.allowed_temp_max,
               w.max_continuous_exceed_seconds, w.max_sample_interval_seconds, ?
        FROM windows AS w
        WHERE NOT EXISTS (
            SELECT 1 FROM rule_revisions AS r
            WHERE r.window_id = w.id AND r.revision = 1
        )
        """,
        (INITIAL_RULE_REASON,),
    )
    conn.commit()


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """写事务：BEGIN IMMEDIATE 立即取写锁，提交/回滚由上下文控制。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：每请求一个独立连接。

    FastAPI 同步端点运行在线程池中，必须使用每请求连接，避免多线程
    共享同一连接造成事务嵌套错误；WAL + BEGIN IMMEDIATE 负责跨连接的
    并发控制。
    """
    conn = connect()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
