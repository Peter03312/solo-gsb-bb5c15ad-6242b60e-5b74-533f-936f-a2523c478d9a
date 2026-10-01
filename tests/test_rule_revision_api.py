"""窗口审定规则版本化修订 HTTP API 集成测试。

运行：PYTHONPATH=. python -m pytest tests/test_rule_revision_api.py -q
"""
from __future__ import annotations

import json
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


def iso(minute: float) -> str:
    t = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=minute)
    return t.isoformat().replace("+00:00", "Z")


RULES_V1 = {
    "allowed_temp_min": 18.0,
    "allowed_temp_max": 22.0,
    "max_continuous_exceed_seconds": 300.0,
    "max_sample_interval_seconds": 600.0,
}


def make_window(client, minutes=10, **overrides):
    body = {
        "name": "case",
        "start_at": iso(0),
        "end_at": iso(minutes),
        **RULES_V1,
        **overrides,
    }
    r = client.post("/api/v1/windows", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def revise(client, wid, rev, reason="收紧温度带", **values):
    body = {"target_revision": rev, "reason": reason, **{**RULES_V1, **values}}
    return client.post(f"/api/v1/windows/{wid}/rule-revisions", json=body)


def submit(client, wid, rid, rev, minute, temp):
    r = client.post(f"/api/v1/windows/{wid}/readings",
                    json={"reading_id": rid, "revision": rev,
                          "measured_at": iso(minute), "temperature": temp})
    assert r.status_code == 201, r.text
    return r.json()


def issue(client, wid, note=None):
    r = client.post(f"/api/v1/windows/{wid}/certificates/issue", json={"note": note})
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------ 初始版本

def test_initial_rules_seeded_as_revision_one(client):
    w = make_window(client)
    assert w["rule_revision"] == 1
    hist = client.get(f"/api/v1/windows/{w['id']}/rule-revisions").json()
    assert len(hist) == 1
    v1 = hist[0]
    assert v1["revision"] == 1
    for k, v in RULES_V1.items():
        assert v1[k] == v
    assert v1["reason"]  # 系统播种说明非空
    assert v1["created_at"]


def test_rule_history_window_not_found_404(client):
    r = client.get("/api/v1/windows/999/rule-revisions")
    assert r.status_code == 404


# ------------------------------------------------------------ 生效与幂等

def test_revision_takes_effect_at_current_plus_one_and_recomputes(client):
    w = make_window(client, max_continuous_exceed_seconds=200.0)
    wid = w["id"]
    # 一条超温读数（23.0 > 22），边界完整、间隔合法；超限段 [0,5)=300s > 200 -> fail
    submit(client, wid, "a", 1, 0, 23.0)
    submit(client, wid, "b", 1, 5, 20.0)
    submit(client, wid, "c", 1, 10, 20.0)
    ev = client.get(f"/api/v1/windows/{wid}/evaluation").json()
    assert ev["verdict"] == "fail"

    # 温度上限放宽到 24：23.0 变为正常 -> pass
    r = revise(client, wid, 2, reason="展品允许更宽温度带", allowed_temp_max=24.0)
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["effect"] == "revised"
    assert d["window_id"] == wid
    assert d["current_rule_revision"] == 2
    assert d["before"]["allowed_temp_max"] == 22.0
    assert d["after"]["allowed_temp_max"] == 24.0
    assert d["after"]["revision"] == 2
    assert d["after"]["reason"] == "展品允许更宽温度带"
    assert d["current_evaluation"]["verdict"] == "pass"
    assert d["current_evaluation"]["max_exceed_seconds"] == 200.0

    # 窗口当前参数与版本号已更新
    detail = client.get(f"/api/v1/windows/{wid}").json()
    assert detail["allowed_temp_max"] == 24.0
    assert detail["rule_revision"] == 2

    # 窗口起止时间与读数保持不变
    assert detail["start_at"] == iso(0) and detail["end_at"] == iso(10)
    readings = client.get(f"/api/v1/windows/{wid}/readings").json()
    assert {x["reading_id"] for x in readings} == {"a", "b", "c"}


def test_revision_history_persists_before_after_reason_time(client):
    w = make_window(client)
    wid = w["id"]
    revise(client, wid, 2, reason="r2", allowed_temp_max=24.0)
    revise(client, wid, 3, reason="r3", max_sample_interval_seconds=900.0)
    hist = client.get(f"/api/v1/windows/{wid}/rule-revisions").json()
    assert [h["revision"] for h in hist] == [1, 2, 3]
    assert hist[1]["allowed_temp_max"] == 24.0 and hist[1]["reason"] == "r2"
    assert hist[2]["max_sample_interval_seconds"] == 900.0 and hist[2]["reason"] == "r3"
    # 相邻版本的“前值”即上一行的值（四项完整留存）
    assert hist[1]["allowed_temp_max"] != hist[0]["allowed_temp_max"]
    assert hist[2]["allowed_temp_max"] == hist[1]["allowed_temp_max"]
    created = [h["created_at"] for h in hist]
    assert created[0] and created[1]


def test_same_revision_identical_values_and_reason_is_idempotent(client):
    w = make_window(client)
    wid = w["id"]
    body = {"target_revision": 2, "reason": "x", **{**RULES_V1, "allowed_temp_max": 24.0}}
    first = client.post(f"/api/v1/windows/{wid}/rule-revisions", json=body)
    assert first.status_code == 201 and first.json()["effect"] == "revised"

    # 完全相同的请求重放 -> idempotent，不新增历史行
    replay = client.post(f"/api/v1/windows/{wid}/rule-revisions", json=body)
    assert replay.status_code == 201, replay.text
    d = replay.json()
    assert d["effect"] == "idempotent"
    assert d["current_rule_revision"] == 2
    assert d["after"]["revision"] == 2
    # before 仍为第 1 版值，after 为第 2 版值
    assert d["before"]["allowed_temp_max"] == 22.0
    assert d["after"]["allowed_temp_max"] == 24.0
    hist = client.get(f"/api/v1/windows/{wid}/rule-revisions").json()
    assert [h["revision"] for h in hist] == [1, 2]


def test_identical_values_new_revision_allowed_even_if_values_unchanged(client):
    """目标号 current+1 即生效，即使四项值与当前版完全相同（仍要求非空原因）。"""
    w = make_window(client)
    wid = w["id"]
    r = revise(client, wid, 2, reason="仅备注，规则值不变")  # 全用 v1 值
    assert r.status_code == 201
    d = r.json()
    assert d["effect"] == "revised" and d["current_rule_revision"] == 2
    assert d["before"] == {k: d["after"][k] for k in RULES_V1}
    hist = client.get(f"/api/v1/windows/{wid}/rule-revisions").json()
    assert [h["revision"] for h in hist] == [1, 2]


# ------------------------------------------------------------ 拒绝路径（409，不留记录）

def test_same_revision_value_conflict_409_no_new_record(client):
    w = make_window(client)
    wid = w["id"]
    revise(client, wid, 2, reason="r2", allowed_temp_max=24.0)
    # 同号（2）但值不同
    r = revise(client, wid, 2, reason="r2", allowed_temp_max=25.0)
    assert r.status_code == 409
    hist = client.get(f"/api/v1/windows/{wid}/rule-revisions").json()
    assert [h["revision"] for h in hist] == [1, 2]
    assert hist[-1]["allowed_temp_max"] == 24.0  # 未被改写


def test_same_revision_reason_conflict_409(client):
    w = make_window(client)
    wid = w["id"]
    revise(client, wid, 2, reason="r2", allowed_temp_max=24.0)
    r = revise(client, wid, 2, reason="不同原因", allowed_temp_max=24.0)
    assert r.status_code == 409 and "一致" in r.json()["detail"]


def test_old_revision_rejected_409(client):
    w = make_window(client)
    wid = w["id"]
    revise(client, wid, 2, allowed_temp_max=24.0)
    revise(client, wid, 3, max_sample_interval_seconds=900.0)
    # 旧号 1、2 都拒绝
    assert revise(client, wid, 1, reason="x", allowed_temp_max=20.0).status_code == 409
    assert revise(client, wid, 2, reason="x", allowed_temp_max=20.0).status_code == 409


def test_target_revision_one_is_always_conflict(client):
    """第 1 版为初始规则：目标号 1 的提交不可能 current+1，一律 409。"""
    w = make_window(client)
    r = revise(client, w["id"], 1, reason="想改初始规则", allowed_temp_max=99.0)
    assert r.status_code == 409


def test_skipped_revision_rejected_409(client):
    """目标号高于当前版 + 1（跳号）-> 409。"""
    w = make_window(client)
    r = revise(client, w["id"], 3, reason="跳号", allowed_temp_max=24.0)
    assert r.status_code == 409 and "连续" in r.json()["detail"]
    # 窗口仍停留在第 1 版
    assert client.get(f"/api/v1/windows/{w['id']}").json()["rule_revision"] == 1


def test_conflict_rolls_back_window_values(client):
    """拒绝不留新记录，且 windows 当前四项参数不被部分改写。"""
    w = make_window(client)
    wid = w["id"]
    # 同号值冲突
    revise(client, wid, 2, reason="r2", allowed_temp_max=24.0)
    client.post(f"/api/v1/windows/{wid}/rule-revisions",
                json={"target_revision": 2, "reason": "r2",
                      **{**RULES_V1, "max_sample_interval_seconds": 123.0}})
    detail = client.get(f"/api/v1/windows/{wid}").json()
    assert detail["allowed_temp_max"] == 24.0
    assert detail["max_sample_interval_seconds"] == 600.0  # 冲突值未落库


def test_revise_missing_window_404(client):
    r = client.post("/api/v1/windows/999/rule-revisions",
                    json={"target_revision": 1, "reason": "x", **RULES_V1})
    assert r.status_code == 404


# ------------------------------------------------------------ 非法输入（422）

@pytest.mark.parametrize("field,value", [
    ("allowed_temp_min", float("nan")),
    ("allowed_temp_max", float("inf")),
    ("allowed_temp_max", float("-inf")),
])
def test_non_finite_numbers_422(client, field, value):
    w = make_window(client)
    raw = json.dumps({"target_revision": 2, "reason": "x", **{**RULES_V1, field: value}})
    r = client.post(f"/api/v1/windows/{w['id']}/rule-revisions", content=raw,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 422


def test_temp_min_greater_than_max_422(client):
    w = make_window(client)
    r = client.post(f"/api/v1/windows/{w['id']}/rule-revisions",
                    json={"target_revision": 2, "reason": "x",
                          **{**RULES_V1,
                             "allowed_temp_min": 30.0, "allowed_temp_max": 25.0}})
    assert r.status_code == 422 and "下限" in r.json()["detail"][0]["msg"]


def test_equal_temp_bounds_allowed(client):
    """下限等于上限合法（闭区间内单一温度）。"""
    w = make_window(client)
    r = client.post(f"/api/v1/windows/{w['id']}/rule-revisions",
                    json={"target_revision": 2, "reason": "恒温展柜",
                          **{**RULES_V1,
                             "allowed_temp_min": 20.0, "allowed_temp_max": 20.0}})
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("field", [
    "max_continuous_exceed_seconds", "max_sample_interval_seconds",
])
@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_durations_422(client, field, bad):
    w = make_window(client)
    r = client.post(f"/api/v1/windows/{w['id']}/rule-revisions",
                    json={"target_revision": 2, "reason": "x",
                          **{**RULES_V1, field: bad}})
    assert r.status_code == 422


@pytest.mark.parametrize("reason", ["", "   ", "\t\n"])
def test_blank_reason_422(client, reason):
    w = make_window(client)
    r = client.post(f"/api/v1/windows/{w['id']}/rule-revisions",
                    json={"target_revision": 2, "reason": reason, **RULES_V1})
    assert r.status_code == 422


def test_missing_fields_422(client):
    w = make_window(client)
    r = client.post(f"/api/v1/windows/{w['id']}/rule-revisions",
                    json={"target_revision": 2, "reason": "x",
                          "allowed_temp_min": 18.0})  # 缺其余三项
    assert r.status_code == 422


def test_target_revision_must_be_positive(client):
    w = make_window(client)
    r = client.post(f"/api/v1/windows/{w['id']}/rule-revisions",
                    json={"target_revision": 0, "reason": "x", **RULES_V1})
    assert r.status_code == 422


# ------------------------------------------------------------ 与签发/证书的关系

def test_existing_certificate_snapshot_unchanged_after_rule_revision(client):
    w = make_window(client)
    wid = w["id"]
    submit(client, wid, "a", 1, 0, 23.0)
    submit(client, wid, "b", 1, 10, 20.0)
    c1 = issue(client, wid, "v1 规则")
    assert c1["snapshot"]["window"]["rule_revision"] == 1

    revise(client, wid, 2, allowed_temp_max=24.0)
    c2 = issue(client, wid, "v2 规则")
    assert c2["version"] == 2
    assert c2["snapshot"]["window"]["rule_revision"] == 2

    # 旧证书快照不变（参数、规则版本、读数、evaluation）
    old = client.get(f"/api/v1/certificates/{c1['id']}").json()
    assert old["snapshot"]["window"]["allowed_temp_max"] == 22.0
    assert old["snapshot"]["window"]["rule_revision"] == 1
    assert old["verdict"] == "fail"


def test_exceed_limit_change_only_affects_verdict_threshold(client):
    """只改“最长连续超限时长”限值：时间线状态不变，但结论可能翻转。"""
    w = make_window(client, max_continuous_exceed_seconds=300.0)
    wid = w["id"]
    # 连续超限 600s
    submit(client, wid, "a", 1, 0, 23.0)
    submit(client, wid, "b", 1, 10, 20.0)
    c1 = issue(client, wid)
    assert c1["verdict"] == "fail"

    # 限值放宽到 600（恰好等于实际连续超限时长）-> 严格大于才算 fail -> pass
    revise(client, wid, 2, reason="放宽超限限值", max_continuous_exceed_seconds=600.0)
    c2 = issue(client, wid)
    assert c2["verdict"] == "pass"
    # 两版 exceeded 区段相同（时间线状态不变）
    assert (c1["snapshot"]["evaluation"]["exceeded_segments"]
            == c2["snapshot"]["evaluation"]["exceeded_segments"])


# ------------------------------------------------------------ 旧库迁移回填

def test_legacy_db_backfills_revision_one(tmp_path, monkeypatch):
    """功能上线前已存在的窗口在启动时补播第 1 版规则。"""
    import sqlite3
    db = tmp_path / "legacy.db"
    # 仅建 windows 表并插入一条窗口（模拟旧库），不建 rule_revisions。
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE windows (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
            start_at TEXT, end_at TEXT,
            allowed_temp_min REAL, allowed_temp_max REAL,
            max_continuous_exceed_seconds REAL, max_sample_interval_seconds REAL,
            created_at TEXT DEFAULT 'x')""")
    conn.execute(
        "INSERT INTO windows (name,start_at,end_at,allowed_temp_min,allowed_temp_max,"
        "max_continuous_exceed_seconds,max_sample_interval_seconds) "
        "VALUES ('old','s','e',17.5,23.5,111,222)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(settings, "db_path", str(db))
    monkeypatch.setattr(settings, "db_timeout", 5)
    from app.main import app

    with TestClient(app) as c:
        detail = c.get("/api/v1/windows/1").json()
        assert detail["rule_revision"] == 1
        hist = c.get("/api/v1/windows/1/rule-revisions").json()
        assert len(hist) == 1 and hist[0]["revision"] == 1
        assert hist[0]["allowed_temp_min"] == 17.5
        assert hist[0]["max_continuous_exceed_seconds"] == 111.0
        assert hist[0]["max_sample_interval_seconds"] == 222.0
