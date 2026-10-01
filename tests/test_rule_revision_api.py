"""窗口规则修订与跨规则版本报告测试。

- HTTP 集成：规则修订（连续修订号/幂等重放/冲突/旧号/跳号/非法输入/404）、
  立即重算、窗口起止与读数不变、既有证书快照不变、后续签发采用新规则、
  规则修订历史、旧库第 1 版回填。
- 报告：跨规则版本时分别依两证快照重建时间线，并给出四项规则前后值；
  即使结论未变也能从 rules_diff 看出规则变更；既有报告字段含义不变。

运行：PYTHONPATH=. python -m pytest tests/test_rule_revision_api.py -q
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "db_timeout", 5)
    from app.main import app

    with TestClient(app) as c:
        yield c


def iso(minute: float, second: float = 0) -> str:
    t = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc) + timedelta(
        minutes=minute, seconds=second)
    return t.isoformat().replace("+00:00", "Z")


RULES_V1 = {
    "allowed_temp_min": 18.0,
    "allowed_temp_max": 22.0,
    "max_continuous_exceed_seconds": 300.0,
    "max_sample_interval_seconds": 600.0,
}


def make_window(client, minutes=10, rules=None, name="case"):
    body = {
        "name": name,
        "start_at": iso(0),
        "end_at": iso(minutes),
        **(rules or RULES_V1),
    }
    r = client.post("/api/v1/windows", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def submit(client, wid, rid, rev, minute, temp, second=0):
    r = client.post(f"/api/v1/windows/{wid}/readings",
                    json={"reading_id": rid, "revision": rev,
                          "measured_at": iso(minute, second), "temperature": temp})
    assert r.status_code == 201, r.text
    return r.json()


def issue(client, wid, note=None):
    r = client.post(f"/api/v1/windows/{wid}/certificates/issue",
                    json={"note": note})
    assert r.status_code == 201, r.text
    return r.json()


def revise(client, wid, rev, rules, reason="季节切换，收窄温度带"):
    return client.post(f"/api/v1/windows/{wid}/rule-revisions",
                       json={"revision": rev, **rules, "reason": reason})


# ------------------------------------------------------------ 初始版本与历史

def test_new_window_has_rule_revision_one(client):
    win = make_window(client)
    assert win["rule_revision"] == 1
    revs = client.get(f"/api/v1/windows/{win['id']}/rule-revisions").json()
    assert len(revs) == 1
    v1 = revs[0]
    assert v1["rule_revision"] == 1
    assert v1["before"] is None
    assert v1["after"] == RULES_V1
    assert v1["reason"]
    assert v1["revised_at"] == win["created_at"]


def test_rule_revisions_missing_window_404(client):
    r = client.post("/api/v1/windows/999/rule-revisions",
                    json={"revision": 1, **RULES_V1, "reason": "x"})
    assert r.status_code == 404
    r = client.get("/api/v1/windows/999/rule-revisions")
    assert r.status_code == 404


# ------------------------------------------------------------ 修订生效

def test_revision_takes_effect_and_recomputes(client):
    win = make_window(client, minutes=10)
    wid = win["id"]
    # a@00:00=20.0 正常；b@00:05=20.0 正常；末读 00:10 终止覆盖
    submit(client, wid, "a", 1, 0, 20.0)
    submit(client, wid, "b", 1, 5, 20.0)
    submit(client, wid, "z", 1, 10, 20.0)
    assert client.get(f"/api/v1/windows/{wid}/evaluation").json()["verdict"] == "pass"

    new_rules = {**RULES_V1, "allowed_temp_max": 19.5}
    r = revise(client, wid, 2, new_rules)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["effect"] == "revised"
    rr = body["rule_revision"]
    assert rr["rule_revision"] == 2
    assert rr["before"] == RULES_V1 and rr["after"] == new_rules
    assert rr["reason"] == "季节切换，收窄温度带"
    assert rr["revised_at"]
    # 20.0 > 19.5：[0,5) 与 [5,10) 均超限共 600s > 300 -> fail
    ev = body["current_evaluation"]
    assert ev["verdict"] == "fail"
    assert ev["max_exceed_seconds"] == 300.0
    assert ev["max_continuous_exceeded_seconds"] == 600.0

    detail = client.get(f"/api/v1/windows/{wid}").json()
    assert detail["rule_revision"] == 2
    assert detail["allowed_temp_max"] == 19.5
    # 窗口起止时间与读数保持不变
    assert detail["start_at"] == win["start_at"]
    assert detail["end_at"] == win["end_at"]
    assert {r["reading_id"] for r in detail["active_readings"]} == {"a", "b", "z"}


def test_consecutive_revisions_must_be_sequential(client):
    wid = make_window(client)["id"]
    r2 = {**RULES_V1, "allowed_temp_max": 21.0}
    r3 = {**RULES_V1, "allowed_temp_max": 20.0}
    assert revise(client, wid, 2, r2).status_code == 201
    assert revise(client, wid, 3, r3).status_code == 201
    revs = client.get(f"/api/v1/windows/{wid}/rule-revisions").json()
    assert [x["rule_revision"] for x in revs] == [1, 2, 3]
    assert revs[2]["before"] == r2 and revs[2]["after"] == r3
    assert client.get(f"/api/v1/windows/{wid}").json()["rule_revision"] == 3


# ------------------------------------------------------------ 幂等与冲突

def test_replay_same_revision_same_values_and_reason_is_idempotent(client):
    wid = make_window(client)["id"]
    new_rules = {**RULES_V1, "max_sample_interval_seconds": 300.0}
    r1 = revise(client, wid, 2, new_rules, reason="仪器校准周期调整")
    r2 = revise(client, wid, 2, new_rules, reason="仪器校准周期调整")
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["effect"] == "revised"
    body2 = r2.json()
    assert body2["effect"] == "idempotent"
    assert body2["rule_revision"] == r1.json()["rule_revision"]
    # 不留新记录：仍只有第 1、2 版
    revs = client.get(f"/api/v1/windows/{wid}/rule-revisions").json()
    assert [x["rule_revision"] for x in revs] == [1, 2]


def test_same_revision_different_values_or_reason_conflicts(client):
    wid = make_window(client)["id"]
    assert revise(client, wid, 2, {**RULES_V1, "allowed_temp_max": 21.0}).status_code == 201
    # 同号、值不同 -> 409
    r = revise(client, wid, 2, {**RULES_V1, "allowed_temp_max": 20.0})
    assert r.status_code == 409 and "冲突" in r.json()["detail"]
    # 同号、值同但原因不同 -> 409
    r = revise(client, wid, 2, {**RULES_V1, "allowed_temp_max": 21.0}, reason="别的原因")
    assert r.status_code == 409 and "冲突" in r.json()["detail"]
    # 当前仍为第 2 版且值未被改动
    detail = client.get(f"/api/v1/windows/{wid}").json()
    assert detail["rule_revision"] == 2 and detail["allowed_temp_max"] == 21.0
    revs = client.get(f"/api/v1/windows/{wid}/rule-revisions").json()
    assert [x["rule_revision"] for x in revs] == [1, 2]


def test_old_revision_rejected_without_new_record(client):
    wid = make_window(client)["id"]
    assert revise(client, wid, 2, {**RULES_V1, "allowed_temp_max": 21.0}).status_code == 201
    # 旧号 1（初始版本号）-> 409
    r = revise(client, wid, 1, RULES_V1, reason="重放初始规则")
    assert r.status_code == 409 and "旧修订号" in r.json()["detail"]
    assert revise(client, wid, 2, {**RULES_V1, "allowed_temp_max": 20.0}).status_code == 409
    revs = client.get(f"/api/v1/windows/{wid}/rule-revisions").json()
    assert [x["rule_revision"] for x in revs] == [1, 2]


def test_skipped_revision_rejected(client):
    wid = make_window(client)["id"]
    r = revise(client, wid, 3, {**RULES_V1, "allowed_temp_max": 21.0})
    assert r.status_code == 409 and "连续递增" in r.json()["detail"]
    # 第 2 版仍未被占用，可正常提交
    assert revise(client, wid, 2, {**RULES_V1, "allowed_temp_max": 21.0}).status_code == 201
    assert client.get(f"/api/v1/windows/{wid}").json()["rule_revision"] == 2


# ------------------------------------------------------------ 非法输入 422

@pytest.mark.parametrize("rules", [
    {"allowed_temp_min": 23.0, "allowed_temp_max": 22.0,
     "max_continuous_exceed_seconds": 300.0, "max_sample_interval_seconds": 600.0},
    {"allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
     "max_continuous_exceed_seconds": 0.0, "max_sample_interval_seconds": 600.0},
    {"allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
     "max_continuous_exceed_seconds": -1.0, "max_sample_interval_seconds": 600.0},
    {"allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
     "max_continuous_exceed_seconds": 300.0, "max_sample_interval_seconds": 0.0},
])
def test_invalid_rules_rejected_422(client, rules):
    wid = make_window(client)["id"]
    r = revise(client, wid, 2, rules)
    assert r.status_code == 422, r.text
    assert client.get(f"/api/v1/windows/{wid}").json()["rule_revision"] == 1


def test_non_finite_values_rejected_422(client):
    wid = make_window(client)["id"]
    for bad in ("Infinity", "-Infinity", "NaN"):
        r = client.post(f"/api/v1/windows/{wid}/rule-revisions",
                        json={"revision": 2, "allowed_temp_min": bad,
                              "allowed_temp_max": 22.0,
                              "max_continuous_exceed_seconds": 300.0,
                              "max_sample_interval_seconds": 600.0,
                              "reason": "x"})
        assert r.status_code == 422, bad
        r = client.post(f"/api/v1/windows/{wid}/rule-revisions",
                        json={"revision": 2, **RULES_V1,
                              "max_continuous_exceed_seconds": bad, "reason": "x"})
        assert r.status_code == 422, bad


def test_blank_reason_rejected_422(client):
    wid = make_window(client)["id"]
    r = revise(client, wid, 2, {**RULES_V1, "allowed_temp_max": 21.0}, reason="   ")
    assert r.status_code == 422


def test_revision_number_must_be_positive(client):
    wid = make_window(client)["id"]
    r = client.post(f"/api/v1/windows/{wid}/rule-revisions",
                    json={"revision": 0, **RULES_V1, "reason": "x"})
    assert r.status_code == 422


# ------------------------------------------------------------ 证书与后续签发

def test_existing_certificate_snapshot_unchanged_after_rule_revision(client):
    wid = make_window(client, minutes=10)["id"]
    submit(client, wid, "a", 1, 0, 20.0)
    submit(client, wid, "b", 1, 5, 20.0)
    submit(client, wid, "z", 1, 10, 20.0)
    c1 = issue(client, wid, "v1-规则第1版")
    assert c1["snapshot"]["window"]["rule_revision"] == 1

    revise(client, wid, 2, {**RULES_V1, "allowed_temp_max": 19.5})
    # 旧证书快照原样：规则值与 rule_revision 不变
    old = client.get(f"/api/v1/certificates/{c1['id']}").json()
    w = old["snapshot"]["window"]
    assert w["rule_revision"] == 1 and w["allowed_temp_max"] == 22.0
    assert old["snapshot"]["evaluation"]["verdict"] == "pass"

    # 后续签发采用新规则
    c2 = issue(client, wid, "v2-规则第2版")
    assert c2["version"] == 2
    assert c2["snapshot"]["window"]["rule_revision"] == 2
    assert c2["snapshot"]["window"]["allowed_temp_max"] == 19.5
    assert c2["verdict"] == "fail"


# ------------------------------------------------------------ 跨规则版本报告

def test_report_across_rule_versions_shows_rules_diff(client):
    wid = make_window(client, minutes=10)["id"]
    submit(client, wid, "a", 1, 0, 20.0)
    submit(client, wid, "b", 1, 5, 20.0)
    submit(client, wid, "z", 1, 10, 20.0)
    c1 = issue(client, wid, "规则1")

    # 只改规则：温度上限 22 -> 19.5；读数不变。两版时间线分别按各自快照
    # 规则重建，[0,10) 由 normal 变 exceeded，但不超时（600s>300 -> fail 实际会变）。
    revise(client, wid, 2, {**RULES_V1, "allowed_temp_max": 19.5})
    c2 = issue(client, wid, "规则2")

    r = client.get("/api/v1/certificates/revision-report",
                   params={"old_certificate_id": c1["id"],
                           "new_certificate_id": c2["id"]})
    assert r.status_code == 200, r.text
    rep = r.json()

    rd = rep["rules_diff"]
    assert rd["rule_revision_before"] == 1 and rd["rule_revision_after"] == 2
    assert rd["rules_changed"] is True
    assert rd["changed_fields"] == ["allowed_temp_max"]
    assert rd["before"]["allowed_temp_max"] == 22.0
    assert rd["after"]["allowed_temp_max"] == 19.5
    ch = rd["changes"][0]
    assert ch["field"] == "allowed_temp_max"
    assert ch["before"] == 22.0 and ch["after"] == 19.5 and ch["delta"] == -2.5
    assert ch["unit_zh"] == "°C"

    # 读数无差异；时间线按两证各自规则重建：[0,5)（a）与 [5,10)（b）
    # 两段 normal -> exceeded（证据读数不同，不合并）
    assert rep["readings_diff"]["changed_count"] == 0
    changes = rep["timeline"]["changes"]
    assert len(changes) == 2
    assert [c["start"] for c in changes] == [iso(0), iso(5)]
    assert all(c["before"]["status"] == "normal" for c in changes)
    assert all(c["after"]["status"] == "exceeded" for c in changes)
    # 时间线头字段沿用新证快照的最大采样间隔（既有字段含义不变）
    assert rep["timeline"]["max_sample_interval_seconds"] == 600.0
    # 汇总既有字段仍在，且结论中文含规则版本变化
    s = rep["summary"]
    assert s["verdict_before"] == "pass" and s["verdict_after"] == "fail"
    assert "第 1 版修订为第 2 版" in s["conclusion_zh"]


def test_report_rules_visible_even_when_verdict_unchanged(client):
    """规则变更但结论不变：rules_diff 仍显示前后值；时间线状态可能不变。"""
    # 限值收紧但读数全部远在区间中央（20.0，新区间 [19.6, 20.4] 仍正常）
    wid = make_window(client, minutes=10)["id"]
    submit(client, wid, "a", 1, 0, 20.0)
    submit(client, wid, "b", 1, 5, 20.0)
    submit(client, wid, "z", 1, 10, 20.0)
    c1 = issue(client, wid)
    revise(client, wid, 2, {**RULES_V1,
                            "allowed_temp_min": 19.6, "allowed_temp_max": 20.4})
    c2 = issue(client, wid)

    rep = client.get("/api/v1/certificates/revision-report",
                     params={"old_certificate_id": c1["id"],
                             "new_certificate_id": c2["id"]}).json()
    rd = rep["rules_diff"]
    assert rd["rules_changed"] is True
    assert rd["changed_fields"] == ["allowed_temp_min", "allowed_temp_max"]
    fields = {c["field"]: c for c in rd["changes"]}
    assert fields["allowed_temp_min"]["delta"] == 1.6
    assert fields["allowed_temp_max"]["delta"] == -1.6
    # 结论未变、时间线无状态变化
    assert rep["summary"]["verdict_changed"] is False
    assert rep["timeline"]["changes"] == []
    assert rep["summary"]["conclusion_zh"].count("规则") >= 1


def test_report_same_rules_no_rule_change(client):
    wid = make_window(client, minutes=10)["id"]
    submit(client, wid, "a", 1, 0, 20.0)
    c1 = issue(client, wid)
    submit(client, wid, "a", 2, 0, 23.0)
    c2 = issue(client, wid)
    rep = client.get("/api/v1/certificates/revision-report",
                     params={"old_certificate_id": c1["id"],
                             "new_certificate_id": c2["id"]}).json()
    rd = rep["rules_diff"]
    assert rd["rules_changed"] is False and rd["changes"] == []
    assert rd["rule_revision_before"] == rd["rule_revision_after"] == 1
    assert "四项规则值无变化" in rep["summary"]["conclusion_zh"]


def test_report_timeline_uses_each_snapshot_own_gap_rule(client):
    """最大采样间隔规则变更：同一点在两证快照中分别判 normal/unknown。"""
    wid = make_window(client, minutes=10,
                      rules={**RULES_V1, "max_sample_interval_seconds": 600.0})["id"]
    # a@00:00 -> b@00:08 间隔 480s
    submit(client, wid, "a", 1, 0, 20.0)
    submit(client, wid, "b", 1, 8, 20.0)
    submit(client, wid, "z", 1, 10, 20.0)
    c1 = issue(client, wid)  # 间隔 480 <= 600 合法 -> 全 normal pass
    assert c1["verdict"] == "pass"

    revise(client, wid, 2, {**RULES_V1, "max_sample_interval_seconds": 300.0})
    c2 = issue(client, wid)  # 480 > 300 -> [0,8) unknown
    assert c2["verdict"] == "pending"

    rep = client.get("/api/v1/certificates/revision-report",
                     params={"old_certificate_id": c1["id"],
                             "new_certificate_id": c2["id"]}).json()
    rd = rep["rules_diff"]
    assert rd["changed_fields"] == ["max_sample_interval_seconds"]
    assert rd["changes"][0]["delta"] == -300.0
    changes = rep["timeline"]["changes"]
    assert len(changes) == 1
    ch = changes[0]
    assert ch["start"] == iso(0) and ch["end"] == iso(8)
    assert ch["before"]["status"] == "normal"
    assert ch["after"]["status"] == "unknown"
    assert ch["after"]["evidence"]["unknown_reason"] == "sample_gap"
    # 新证限值体现在时间线头字段
    assert rep["timeline"]["max_sample_interval_seconds"] == 300.0


# ------------------------------------------------------------ 旧库迁移

def test_legacy_window_without_rule_revisions_backfilled(tmp_path, monkeypatch):
    """旧库（rule_revisions 表出现前创建的窗口）启动后补齐第 1 版。"""
    db_file = tmp_path / "legacy.db"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "db_timeout", 5)

    # 用旧结构直接建一个窗口（无 rule_revisions 表）
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE windows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, start_at TEXT NOT NULL, end_at TEXT NOT NULL,
            allowed_temp_min REAL NOT NULL, allowed_temp_max REAL NOT NULL,
            max_continuous_exceed_seconds REAL NOT NULL,
            max_sample_interval_seconds REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        CREATE TABLE readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            window_id INTEGER NOT NULL REFERENCES windows(id),
            reading_id TEXT NOT NULL, revision INTEGER NOT NULL,
            measured_at TEXT NOT NULL, temperature REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE(window_id, reading_id), UNIQUE(window_id, measured_at)
        );
        CREATE TABLE reading_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            window_id INTEGER NOT NULL, reading_id TEXT NOT NULL,
            revision INTEGER NOT NULL, measured_at TEXT NOT NULL,
            temperature REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active',
            void_reason TEXT, voided_at TEXT, prev_revision INTEGER,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE(window_id, reading_id, revision)
        );
        CREATE TABLE certificates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            window_id INTEGER NOT NULL REFERENCES windows(id),
            version INTEGER NOT NULL, verdict TEXT NOT NULL,
            snapshot_json TEXT NOT NULL, note TEXT,
            issued_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE(window_id, version)
        );
        """
    )
    conn.execute(
        "INSERT INTO windows (name, start_at, end_at, allowed_temp_min, "
        "allowed_temp_max, max_continuous_exceed_seconds, "
        "max_sample_interval_seconds) VALUES ('legacy', ?, ?, 18, 22, 300, 600)",
        (iso(0), iso(10)),
    )
    conn.commit()
    conn.close()

    from app.main import app
    with TestClient(app) as c:
        detail = c.get("/api/v1/windows/1").json()
        assert detail["rule_revision"] == 1
        revs = c.get("/api/v1/windows/1/rule-revisions").json()
        assert [x["rule_revision"] for x in revs] == [1]
        assert revs[0]["before"] is None
        assert revs[0]["after"] == RULES_V1
        # 回填后可正常提交第 2 版
        r = c.post("/api/v1/windows/1/rule-revisions",
                   json={"revision": 2, **RULES_V1,
                         "allowed_temp_max": 21.0, "reason": "迁移后修订"})
        assert r.status_code == 201, r.text
        assert r.json()["effect"] == "revised"
        # 再启动一次不重复回填
    with TestClient(app) as c:
        revs = c.get("/api/v1/windows/1/rule-revisions").json()
        assert [x["rule_revision"] for x in revs] == [1, 2]
