"""HTTP API 集成测试：窗口、读数幂等/修订、拒收、审定、签发版本、并发。

运行：PYTHONPATH=. python -m pytest tests/test_api.py -q
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "db_timeout", 5)
    # 延迟导入 app，确保 connect() 读取被覆盖后的 settings
    from app.main import app

    with TestClient(app) as c:
        yield c


WINDOW_BASE = "2026-10-01T00:00:00Z"


def make_window(client, start=WINDOW_BASE, minutes=60, tmin=18.0, tmax=22.0,
                max_exceed=300.0, max_gap=120.0, name=None):
    body = {
        "start_at": start,
        "end_at": (datetime.fromisoformat(start.replace("Z", "+00:00"))
                   + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"),
        "allowed_temp_min": tmin,
        "allowed_temp_max": tmax,
        "max_continuous_exceed_seconds": max_exceed,
        "max_sample_interval_seconds": max_gap,
    }
    if name:
        body["name"] = name
    r = client.post("/api/v1/windows", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def reading(rid, rev, minute, temp):
    ts = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=minute)
    return {
        "reading_id": rid, "revision": rev,
        "measured_at": ts.isoformat().replace("+00:00", "Z"),
        "temperature": temp,
    }


# ---------------------------------------------------------------- 健康检查/元数据

def test_health_and_root(client):
    assert client.get("/health").json() == {"status": "ok"}
    root = client.get("/").json()
    assert root["service"] == "museum-case-temperature-audit"


# ---------------------------------------------------------------- 窗口校验

def test_invalid_window_reversed(client):
    r = client.post("/api/v1/windows", json={
        "start_at": "2026-10-01T01:00:00Z", "end_at": "2026-10-01T00:00:00Z",
        "allowed_temp_min": 18, "allowed_temp_max": 22,
        "max_continuous_exceed_seconds": 300, "max_sample_interval_seconds": 120})
    assert r.status_code == 422
    assert "无效窗口" in r.json()["detail"]


def test_invalid_window_temp_range(client):
    r = client.post("/api/v1/windows", json={
        "start_at": "2026-10-01T00:00:00Z", "end_at": "2026-10-01T01:00:00Z",
        "allowed_temp_min": 23, "allowed_temp_max": 22,
        "max_continuous_exceed_seconds": 300, "max_sample_interval_seconds": 120})
    assert r.status_code == 422


def test_naive_timestamp_rejected(client):
    make_window(client)
    r = client.post("/api/v1/windows/1/readings",
                    json={"reading_id": "a", "revision": 1,
                          "measured_at": "2026-10-01T00:00:00", "temperature": 20.0})
    assert r.status_code == 422


def test_get_missing_window_404(client):
    assert client.get("/api/v1/windows/999").status_code == 404


# ---------------------------------------------------------------- 读数拒收

def test_reading_outside_window_rejected(client):
    make_window(client)
    r = client.post("/api/v1/windows/1/readings", json=reading("a", 1, -1, 20.0))
    assert r.status_code == 422 and "超出窗口" in r.json()["detail"]
    r = client.post("/api/v1/windows/1/readings", json=reading("a", 1, 61, 20.0))
    assert r.status_code == 422


def test_reading_at_window_end_accepted(client):
    make_window(client)
    r = client.post("/api/v1/windows/1/readings", json=reading("a", 1, 60, 20.0))
    assert r.status_code == 201, r.text


def test_same_timestamp_different_reading_rejected(client):
    make_window(client)
    assert client.post("/api/v1/windows/1/readings", json=reading("a", 1, 0, 20.0)).status_code == 201
    r = client.post("/api/v1/windows/1/readings", json=reading("b", 1, 0, 21.0))
    assert r.status_code == 422 and "同刻" in r.json()["detail"]


# ---------------------------------------------------------------- 幂等与修订

def test_idempotent_same_id_revision(client):
    make_window(client)
    body = reading("a", 1, 0, 20.0)
    r1 = client.post("/api/v1/windows/1/readings", json=body)
    r2 = client.post("/api/v1/windows/1/readings", json=body)
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["effect"] == "created"
    assert r2.json()["effect"] == "idempotent"
    assert r1.json()["reading"] == r2.json()["reading"]
    # 活动读数只有一条
    rows = client.get("/api/v1/windows/1/readings").json()
    assert len(rows) == 1 and rows[0]["revision"] == 1


def test_conflict_same_id_revision_different_content(client):
    make_window(client)
    client.post("/api/v1/windows/1/readings", json=reading("a", 1, 0, 20.0))
    # 同 ID 同修订号、不同温度 -> 409
    r = client.post("/api/v1/windows/1/readings", json=reading("a", 1, 0, 21.0))
    assert r.status_code == 409 and "内容冲突" in r.json()["detail"]
    # 同 ID 同修订号、不同时间戳 -> 409
    r = client.post("/api/v1/windows/1/readings", json=reading("a", 1, 5, 20.0))
    assert r.status_code == 409


def test_lower_revision_rejected(client):
    make_window(client)
    client.post("/api/v1/windows/1/readings", json=reading("a", 2, 0, 20.0))
    r = client.post("/api/v1/windows/1/readings", json=reading("a", 1, 0, 20.0))
    assert r.status_code == 409 and "旧修订号" in r.json()["detail"]


def test_higher_revision_replaces_and_recomputes(client):
    make_window(client, max_gap=600.0)
    # 00:01->00:08 连续超限 420s > 300 -> fail
    client.post("/api/v1/windows/1/readings", json=reading("a", 1, 0, 20.0))
    client.post("/api/v1/windows/1/readings", json=reading("b", 1, 1, 23.0))
    client.post("/api/v1/windows/1/readings", json=reading("c", 1, 7, 23.0))
    client.post("/api/v1/windows/1/readings", json=reading("d", 1, 8, 20.0))
    before = client.get("/api/v1/windows/1/evaluation").json()
    assert before["verdict"] == "fail"
    assert before["max_continuous_exceeded_seconds"] == 420.0

    # 修订 b 到 rev2：温度改回 20.0（在 00:01），超限只剩 c 的 60s
    r = client.post("/api/v1/windows/1/readings", json=reading("b", 2, 1, 20.0))
    assert r.status_code == 201 and r.json()["effect"] == "updated"
    after = r.json()["current_evaluation"]
    assert after["verdict"] == "pending"
    assert after["max_continuous_exceeded_seconds"] == 60.0

    rows = client.get("/api/v1/windows/1/readings").json()
    rev_map = {x["reading_id"]: x["revision"] for x in rows}
    assert rev_map == {"a": 1, "b": 2, "c": 1, "d": 1}

    # 旧修订号再次提交仍被拒绝
    r = client.post("/api/v1/windows/1/readings", json=reading("b", 1, 1, 23.0))
    assert r.status_code == 409


def test_revision_cannot_move_timestamp_onto_another_reading(client):
    make_window(client)
    client.post("/api/v1/windows/1/readings", json=reading("a", 1, 0, 20.0))
    client.post("/api/v1/windows/1/readings", json=reading("b", 1, 5, 20.0))
    # 修订 b，把时间戳挪到 a 的时刻 -> 拒收
    r = client.post("/api/v1/windows/1/readings", json=reading("b", 2, 0, 20.0))
    assert r.status_code == 422
    # b 仍保留旧值
    rows = {x["reading_id"]: x for x in client.get("/api/v1/windows/1/readings").json()}
    assert rows["b"]["revision"] == 1


# ---------------------------------------------------------------- 结论

def test_full_coverage_pass(client):
    win = make_window(client, max_gap=120.0)
    wid = win["id"]
    for m in range(61):
        r = client.post(f"/api/v1/windows/{wid}/readings",
                        json=reading(f"r{m:02d}", 1, m, 20.0))
        assert r.status_code == 201
    ev = client.get(f"/api/v1/windows/{wid}/evaluation").json()
    assert ev["verdict"] == "pass"
    assert not ev["unknown_segments"]


def test_fail_takes_priority_over_unknown(client):
    make_window(client, max_gap=600.0)
    # 即使尾部未知，超限 400 > 300 仍判 fail
    client.post("/api/v1/windows/1/readings", json=reading("a", 1, 0, 20.0))
    client.post("/api/v1/windows/1/readings", json=reading("b", 1, 1, 23.0))
    client.post("/api/v1/windows/1/readings", json=reading("c", 1, 3, 24.0))
    # 00:07:40 的读数，使连续超限区间为 00:01 -> 00:07:40 = 400s
    ts = datetime(2026, 10, 1, 0, 7, 40, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    client.post("/api/v1/windows/1/readings",
                json={"reading_id": "d", "revision": 1, "measured_at": ts, "temperature": 20.0})
    ev = client.get("/api/v1/windows/1/evaluation").json()
    assert ev["verdict"] == "fail"
    assert ev["max_continuous_exceeded_seconds"] == 400.0


# ---------------------------------------------------------------- 签发版本与快照

def _seed_pass_window(client):
    win = make_window(client)
    wid = win["id"]
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 1, 0, 23.0))
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("b", 1, 2, 20.0))
    return wid


def test_certificate_versions_and_immutable_snapshot(client):
    wid = _seed_pass_window(client)
    ev0 = client.get(f"/api/v1/windows/{wid}/evaluation").json()
    assert ev0["verdict"] == "pending"  # 00:00-00:02 超限但仅 120s，其余未知

    # v1：pending
    c1 = client.post(f"/api/v1/windows/{wid}/certificates/issue", json={"note": "首签"}).json()
    assert c1["version"] == 1 and c1["verdict"] == "pending"
    assert c1["snapshot"]["evaluation"]["verdict"] == "pending"
    assert len(c1["snapshot"]["readings"]) == 2

    # 修订读数：补齐整窗、全部正常
    # 删除不了，改为新建另一个窗口做 pass；这里用追加读数让尾部未知消失
    for m in range(3, 61):
        client.post(f"/api/v1/windows/{wid}/readings",
                    json=reading(f"r{m:02d}", 1, m, 20.0))
    # a 超温仍在（00:00-00:02 120s < 300），但无未知区段后 -> pass？
    # 有 exceeded 区段但不超最长连续限值：按规则“超过限值判不合格；否则有未知待定；其余合格”
    ev1 = client.get(f"/api/v1/windows/{wid}/evaluation").json()
    assert ev1["verdict"] == "pass", ev1
    assert ev1["max_continuous_exceeded_seconds"] == 120.0

    # v2：修订 a 到正常温度 rev2，使全部正常后再签发 pass 版本
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 2, 0, 20.0))
    c2 = client.post(f"/api/v1/windows/{wid}/certificates/issue", json={"note": "修订后重签"}).json()
    assert c2["version"] == 2 and c2["verdict"] == "pass"
    assert c2["snapshot"]["readings"][0]["revision"] == 2

    # 旧证书 v1 快照原样保留
    old = client.get(f"/api/v1/certificates/{c1['id']}").json()
    assert old["version"] == 1 and old["verdict"] == "pending"
    assert old["snapshot"]["readings"][0]["revision"] == 1
    assert old["note"] == "首签"

    versions = client.get(f"/api/v1/windows/{wid}/certificates").json()
    assert [c["version"] for c in versions] == [1, 2]

    detail = client.get(f"/api/v1/windows/{wid}").json()
    assert detail["latest_certificate_version"] == 2
    assert detail["latest_verdict"] == "pass"
    assert detail["current_evaluation"]["verdict"] == "pass"


def test_issue_missing_window_404(client):
    r = client.post("/api/v1/windows/42/certificates/issue", json={})
    assert r.status_code == 404


# ---------------------------------------------------------------- 并发

def test_concurrent_revisions_serialized(client):
    """两个线程同时对同一读数提交 rev2/rev3：BEGIN IMMEDIATE 串行化两种可能次序——
    先 rev2 后 rev3（两次都 updated），或先 rev3 后 rev2（rev3 updated、rev2 被拒）。
    两种情况都必须不出现 500，且最终活动读数为 rev3。"""
    wid = make_window(client)["id"]
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 1, 0, 20.0))

    results = []

    def submit(rev, temp, barrier):
        c = TestClient(client.app)
        barrier.wait()
        r = c.post(f"/api/v1/windows/{wid}/readings",
                   json=reading("a", rev, 0, temp))
        results.append((rev, r.status_code, r.json().get("effect")))

    barrier = threading.Barrier(2)
    t1 = threading.Thread(target=submit, args=(2, 20.5, barrier))
    t2 = threading.Thread(target=submit, args=(3, 20.6, barrier))
    t1.start(); t2.start(); t1.join(); t2.join()

    # rev3 必须成功；rev2 要么先于 rev3 落库（201 updated），要么被更高修订号拒绝（409）
    by_rev = {rev: (code, effect) for rev, code, effect in results}
    assert by_rev[3][0] == 201, results
    assert by_rev[2][0] in (201, 409), results
    assert all(code < 500 for _, code, _ in results)
    rows = client.get(f"/api/v1/windows/{wid}/readings").json()
    assert rows[0]["revision"] == 3 and rows[0]["temperature"] == 20.6


def test_concurrent_issue_and_revision_use_committed_state(client):
    """签发与修订并发：两种串行化结果都必须自洽——
    快照要么是 rev1（之后仍可用 rev2 重出新版本），要么直接是 rev2。"""
    wid = make_window(client)["id"]
    client.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 1, 0, 23.0))
    # 填满窗口且全部正常，仅 a 在 rev1 超温 120s（<300）
    for m in range(1, 61):
        client.post(f"/api/v1/windows/{wid}/readings", json=reading(f"r{m:02d}", 1, m, 20.0))

    issued = {}

    def issue(barrier):
        c = TestClient(client.app)
        barrier.wait()
        r = c.post(f"/api/v1/windows/{wid}/certificates/issue", json={})
        issued["code"] = r.status_code
        issued["body"] = r.json()

    def revise(barrier):
        c = TestClient(client.app)
        barrier.wait()
        r = c.post(f"/api/v1/windows/{wid}/readings", json=reading("a", 2, 0, 20.0))
        issued["revise_code"] = r.status_code

    barrier = threading.Barrier(2)
    ti = threading.Thread(target=issue, args=(barrier,))
    tr = threading.Thread(target=revise, args=(barrier,))
    ti.start(); tr.start(); ti.join(); tr.join()

    assert issued["code"] == 201 and issued["revise_code"] == 201
    snap_rev = issued["body"]["snapshot"]["readings"][0]["revision"]
    assert snap_rev in (1, 2)
    # 再签发一版，必然包含 rev2，且版本号递增；旧快照不变
    c2 = client.post(f"/api/v1/windows/{wid}/certificates/issue", json={}).json()
    assert c2["version"] == issued["body"]["version"] + 1
    assert c2["snapshot"]["readings"][0]["revision"] == 2
    old = client.get(f"/api/v1/certificates/{issued['body']['id']}").json()
    assert old["snapshot"]["readings"][0]["revision"] == snap_rev


# ---------------------------------------------------------------- 分页/列表

def test_list_windows_paging(client):
    for i in range(3):
        make_window(client, name=f"case-{i}")
    page = client.get("/api/v1/windows?limit=2&offset=0").json()
    assert len(page) == 2
    assert client.get("/api/v1/windows?limit=2&offset=2").json()[0]["name"] == "case-0"
