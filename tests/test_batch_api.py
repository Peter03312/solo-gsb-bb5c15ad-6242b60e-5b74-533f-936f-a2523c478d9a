"""HTTP API 集成测试：采样仪器批量补传（整批事务、最终状态同刻唯一、互换时刻）。

运行：PYTHONPATH=. python -m pytest tests/test_batch_api.py -q
"""
from __future__ import annotations

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
BATCH = "/api/v1/windows/1/readings/batch"


def make_window(client, start=WINDOW_BASE, minutes=60, tmin=18.0, tmax=22.0,
                max_exceed=300.0, max_gap=600.0, name=None):
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


def reading(rid, rev, minute, temp, *, second=0):
    ts = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=minute, seconds=second)
    return {
        "reading_id": rid, "revision": rev,
        "measured_at": ts.isoformat().replace("+00:00", "Z"),
        "temperature": temp,
    }


def submit_one(client, wid, body):
    return client.post(f"/api/v1/windows/{wid}/readings", json=body)


def submit_batch(client, wid, items):
    return client.post(f"/api/v1/windows/{wid}/readings/batch", json={"readings": items})


def active_map(client, wid):
    return {r["reading_id"]: r
            for r in client.get(f"/api/v1/windows/{wid}/readings").json()}


# ---------------------------------------------------------------- 基本批次

def test_batch_create_multiple_and_recompute(client):
    make_window(client, max_gap=600.0)
    items = [
        reading("a", 1, 0, 20.0),
        reading("b", 1, 10, 23.0),
        reading("c", 1, 20, 20.0),
    ]
    r = submit_batch(client, 1, items)
    assert r.status_code == 201, r.text
    body = r.json()
    effects = [x["effect"] for x in body["results"]]
    assert effects == ["created", "created", "created"]
    ids = [x["reading"]["reading_id"] for x in body["results"]]
    assert ids == ["a", "b", "c"]
    # 按批次最终状态重算：00:10->00:20 超限 600s > 300 -> fail
    assert body["current_evaluation"]["verdict"] == "fail"
    assert body["current_evaluation"]["max_continuous_exceeded_seconds"] == 600.0
    assert body["current_evaluation"]["reading_count"] == 3


def test_empty_batch_rejected(client):
    make_window(client)
    r = submit_batch(client, 1, [])
    assert r.status_code == 422


def test_batch_missing_window_404(client):
    make_window(client)
    r = submit_batch(client, 999, [reading("a", 1, 0, 20.0)])
    assert r.status_code == 404


def test_batch_invalid_item_shape_rejected(client):
    make_window(client)
    # 缺时区
    r = submit_batch(client, 1, [
        {"reading_id": "a", "revision": 1,
         "measured_at": "2026-10-01T00:00:00", "temperature": 20.0},
    ])
    assert r.status_code == 422
    assert client.get("/api/v1/windows/1/readings").json() == []


# ---------------------------------------------------------------- 整批原子性

def test_batch_duplicate_reading_id_rejected_and_atomic(client):
    make_window(client)
    r = submit_batch(client, 1, [
        reading("a", 1, 0, 20.0),
        reading("a", 1, 5, 21.0),
    ])
    assert r.status_code == 422 and "重复" in r.json()["detail"]
    assert client.get("/api/v1/windows/1/readings").json() == []


def test_batch_outside_window_rejected_and_atomic(client):
    make_window(client)
    r = submit_batch(client, 1, [
        reading("a", 1, 0, 20.0),
        reading("b", 1, 61, 20.0),  # 超出窗口终点
    ])
    assert r.status_code == 422 and "超出窗口" in r.json()["detail"]
    # 整批拒收：a 也不得落库
    assert client.get("/api/v1/windows/1/readings").json() == []


def test_batch_same_timestamp_final_state_rejected_and_atomic(client):
    make_window(client)
    r = submit_batch(client, 1, [
        reading("a", 1, 0, 20.0),
        reading("b", 1, 0, 21.0),
    ])
    assert r.status_code == 422 and "同刻" in r.json()["detail"]
    assert client.get("/api/v1/windows/1/readings").json() == []


def test_batch_conflict_does_not_change_active_or_history(client):
    make_window(client)
    submit_one(client, 1, reading("a", 1, 0, 20.0))
    before = active_map(client, 1)

    # 第二项与既有活动读数同刻；同时第一项是高修订号更新（本应成功）
    r = submit_batch(client, 1, [
        reading("a", 2, 0, 20.5),
        reading("b", 1, 0, 19.0),
    ])
    assert r.status_code == 422
    after = active_map(client, 1)
    # 活动读数不变
    assert after == before
    # 修订历史未新增 rev2
    from app.database import connect
    conn = connect()
    try:
        revs = conn.execute(
            "SELECT revision, status FROM reading_revisions "
            "WHERE window_id=1 AND reading_id='a' ORDER BY revision"
        ).fetchall()
        assert [(row["revision"], row["status"]) for row in revs] == [(1, "active")]
    finally:
        conn.close()


def test_batch_content_conflict_409_and_atomic(client):
    make_window(client)
    submit_one(client, 1, reading("a", 1, 0, 20.0))
    r = submit_batch(client, 1, [
        reading("a", 1, 0, 99.9),         # 同 ID 同修订号内容冲突 -> 409
        reading("b", 1, 5, 20.0),         # 本应 created
    ])
    assert r.status_code == 409 and "内容冲突" in r.json()["detail"]
    assert set(active_map(client, 1)) == {"a"}
    assert active_map(client, 1)["a"]["temperature"] == 20.0


def test_batch_stale_revision_409_and_atomic(client):
    make_window(client)
    submit_one(client, 1, reading("a", 2, 0, 20.0))
    r = submit_batch(client, 1, [
        reading("a", 1, 0, 20.0),         # 旧修订号 -> 409
        reading("b", 1, 5, 20.0),
    ])
    assert r.status_code == 409 and "旧修订号" in r.json()["detail"]
    assert set(active_map(client, 1)) == {"a"}
    assert active_map(client, 1)["a"]["revision"] == 2


# ---------------------------------------------------------------- 互换时刻

def test_batch_swap_timestamps_with_higher_revisions(client):
    make_window(client)
    submit_one(client, 1, reading("a", 1, 0, 20.0))
    submit_one(client, 1, reading("b", 1, 10, 21.0))

    # 两条都以更高修订号互换采样时刻：a -> 00:10，b -> 00:00
    r = submit_batch(client, 1, [
        reading("a", 2, 10, 20.0),
        reading("b", 2, 0, 21.0),
    ])
    assert r.status_code == 201, r.text
    assert [x["effect"] for x in r.json()["results"]] == ["updated", "updated"]
    rows = active_map(client, 1)
    assert rows["a"]["revision"] == 2 and rows["a"]["measured_at"].endswith("T00:10:00Z")
    assert rows["b"]["revision"] == 2 and rows["b"]["measured_at"].endswith("T00:00:00Z")
    # 旧修订号在历史中标记 superseded
    from app.database import connect
    conn = connect()
    try:
        revs = conn.execute(
            "SELECT reading_id, revision, status FROM reading_revisions WHERE window_id=1 "
            "ORDER BY reading_id, revision"
        ).fetchall()
        got = {(row["reading_id"], row["revision"]): row["status"] for row in revs}
        assert got == {("a", 1): "superseded", ("a", 2): "active",
                       ("b", 1): "superseded", ("b", 2): "active"}
    finally:
        conn.close()


def test_batch_swap_regardless_of_order(client):
    make_window(client)
    submit_one(client, 1, reading("a", 1, 0, 20.0))
    submit_one(client, 1, reading("b", 1, 10, 21.0))
    # 请求顺序反过来也不得误拒
    r = submit_batch(client, 1, [
        reading("b", 2, 0, 21.0),
        reading("a", 2, 10, 20.0),
    ])
    assert r.status_code == 201, r.text
    rows = active_map(client, 1)
    assert rows["a"]["measured_at"].endswith("T00:10:00Z")
    assert rows["b"]["measured_at"].endswith("T00:00:00Z")


def test_batch_cannot_swap_one_onto_unmoved_reading(client):
    make_window(client)
    submit_one(client, 1, reading("a", 1, 0, 20.0))
    submit_one(client, 1, reading("b", 1, 10, 21.0))
    # 只把 a 挪到 b 的时刻，b 不动 -> 最终状态仍同刻，整批拒收
    r = submit_batch(client, 1, [reading("a", 2, 10, 20.0)])
    assert r.status_code == 422 and "同刻" in r.json()["detail"]
    rows = active_map(client, 1)
    assert rows["a"]["revision"] == 1 and rows["a"]["measured_at"].endswith("T00:00:00Z")


# ---------------------------------------------------------------- 幂等重放

def test_batch_mixed_created_updated_idempotent(client):
    make_window(client)
    submit_one(client, 1, reading("a", 1, 0, 20.0))
    submit_one(client, 1, reading("b", 1, 10, 20.0))

    r = submit_batch(client, 1, [
        reading("a", 1, 0, 20.0),    # idempotent
        reading("b", 2, 10, 21.0),   # updated
        reading("c", 1, 20, 20.0),   # created
    ])
    assert r.status_code == 201, r.text
    assert [x["effect"] for x in r.json()["results"]] == \
           ["idempotent", "updated", "created"]


def test_batch_replay_creates_no_new_revision(client):
    make_window(client)
    items = [reading("a", 1, 0, 20.0), reading("b", 1, 10, 20.0)]
    r1 = submit_batch(client, 1, items)
    assert r1.status_code == 201
    r2 = submit_batch(client, 1, items)
    assert r2.status_code == 201, r2.text
    assert [x["effect"] for x in r2.json()["results"]] == ["idempotent", "idempotent"]
    # 读数完全一致，审定一致
    assert r1.json()["current_evaluation"] == r2.json()["current_evaluation"]

    from app.database import connect
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM reading_revisions WHERE window_id=1"
        ).fetchone()["c"]
        assert n == 2  # 重放不产生新修订历史行
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM reading_revisions "
            "WHERE window_id=1 GROUP BY status"
        ).fetchall()
        assert {row["status"]: row["c"] for row in rows} == {"active": 2}
    finally:
        conn.close()


def test_batch_partial_replay_after_update(client):
    make_window(client)
    submit_batch(client, 1, [reading("a", 1, 0, 20.0), reading("b", 1, 10, 20.0)])
    # 第二批：更新 b 后立即重放同一批次，b 应幂等，不产生 rev3
    r = submit_batch(client, 1, [
        reading("a", 1, 0, 20.0),
        reading("b", 2, 10, 21.0),
    ])
    assert [x["effect"] for x in r.json()["results"]] == ["idempotent", "updated"]
    r2 = submit_batch(client, 1, [
        reading("a", 1, 0, 20.0),
        reading("b", 2, 10, 21.0),
    ])
    assert [x["effect"] for x in r2.json()["results"]] == ["idempotent", "idempotent"]


# ---------------------------------------------------------------- 与既有能力共存

def test_batch_then_single_submit_still_works(client):
    make_window(client)
    submit_batch(client, 1, [reading("a", 1, 0, 20.0)])
    r = submit_one(client, 1, reading("b", 1, 10, 20.0))
    assert r.status_code == 201 and r.json()["effect"] == "created"
    # 单条修订批内读数也可用
    r = submit_one(client, 1, reading("a", 2, 0, 20.1))
    assert r.status_code == 201 and r.json()["effect"] == "updated"


def test_batch_does_not_change_issued_snapshot(client):
    make_window(client, max_gap=600.0)
    submit_batch(client, 1, [
        reading("a", 1, 0, 23.0),
        reading("b", 1, 10, 20.0),
    ])
    cert = client.post("/api/v1/windows/1/certificates/issue",
                       json={"note": "批后首签"}).json()
    assert cert["version"] == 1
    snap_revs = {r["reading_id"]: r["revision"] for r in cert["snapshot"]["readings"]}
    assert snap_revs == {"a": 1, "b": 1}

    # 补传修订后，已签发快照不变
    submit_batch(client, 1, [reading("a", 2, 0, 20.0)])
    old = client.get(f"/api/v1/certificates/{cert['id']}").json()
    old_revs = {r["reading_id"]: r["revision"] for r in old["snapshot"]["readings"]}
    assert old_revs == {"a": 1, "b": 1}
    assert old["verdict"] == cert["verdict"]

    # 当前审定按最终状态重算，再次签发形成新版本
    cur = client.get("/api/v1/windows/1/evaluation").json()
    assert cur["reading_count"] == 2
    c2 = client.post("/api/v1/windows/1/certificates/issue", json={}).json()
    assert c2["version"] == 2


def test_batch_timezone_normalized_like_single(client):
    make_window(client)
    # +08:00 表示 2026-10-01T08:00+08:00 == 00:00Z
    r = submit_batch(client, 1, [
        {"reading_id": "a", "revision": 1,
         "measured_at": "2026-10-01T08:00:00+08:00", "temperature": 20.0},
    ])
    assert r.status_code == 201, r.text
    out = r.json()["results"][0]["reading"]
    assert out["measured_at"] == "2026-10-01T00:00:00Z"


def test_batch_results_order_matches_request(client):
    make_window(client)
    items = [reading("c", 1, 20, 20.0), reading("a", 1, 0, 20.0),
             reading("b", 1, 10, 20.0)]
    r = submit_batch(client, 1, items)
    assert [x["reading"]["reading_id"] for x in r.json()["results"]] == ["c", "a", "b"]
