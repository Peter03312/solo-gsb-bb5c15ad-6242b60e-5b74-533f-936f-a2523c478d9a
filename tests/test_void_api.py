"""HTTP API 集成测试：误采作废与恢复（void/restore）。

覆盖：
- 作废：剔除活动读数并立即重算；作废修订留存原因/操作时间/前一版本；
  相同修订号+原因重放幂等；原因不同或低修订号 409；从未存在 404；
  已作废不能再次以新修订号作废；非空原因校验。
- 恢复：单条/批量提交更高修订号恢复（restored），遵守窗口边界与最终状态
  同刻唯一；旧版提交不得复活；批量任一项冲突整批回滚。
- 证书与报告：已签发快照不变，后续签发反映作废/恢复，修订影响报告呈现
  读数移除（removed）与再出现（added）。

运行：PYTHONPATH=. python -m pytest tests/test_void_api.py -q
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


def reading(rid, rev, minute, temp):
    ts = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=minute)
    return {
        "reading_id": rid, "revision": rev,
        "measured_at": ts.isoformat().replace("+00:00", "Z"),
        "temperature": temp,
    }


def submit_one(client, wid, body):
    return client.post(f"/api/v1/windows/{wid}/readings", json=body)


def submit_batch(client, wid, items):
    return client.post(f"/api/v1/windows/{wid}/readings/batch", json={"readings": items})


def void(client, wid, rid, rev, reason):
    return client.post(f"/api/v1/windows/{wid}/readings/{rid}/void",
                       json={"revision": rev, "reason": reason})


def active_map(client, wid):
    return {r["reading_id"]: r
            for r in client.get(f"/api/v1/windows/{wid}/readings").json()}


def history_rows(wid, rid):
    from app.database import connect
    conn = connect()
    try:
        return conn.execute(
            "SELECT revision, status, void_reason, prev_revision FROM reading_revisions "
            "WHERE window_id=? AND reading_id=? ORDER BY revision",
            (wid, rid),
        ).fetchall()
    finally:
        conn.close()


def seed_window(client, max_gap=600.0):
    """三条读数：a@00:00=20.0，b@00:10=23.0（超限），c@00:20=20.0。"""
    make_window(client, max_gap=max_gap)
    submit_one(client, 1, reading("a", 1, 0, 20.0))
    submit_one(client, 1, reading("b", 1, 10, 23.0))
    submit_one(client, 1, reading("c", 1, 20, 20.0))


# ---------------------------------------------------------------- 作废基本语义

def test_void_removes_reading_and_recomputes(client):
    seed_window(client)
    before = client.get("/api/v1/windows/1/evaluation").json()
    assert before["verdict"] == "fail"  # b 在 00:10->00:20 超限 600s > 300
    assert before["reading_count"] == 3

    r = void(client, 1, "b", 2, "误采：传感器未校准")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["effect"] == "voided"
    rec = body["void"]
    assert rec["reading_id"] == "b" and rec["revision"] == 2
    assert rec["status"] == "voided"
    assert rec["reason"] == "误采：传感器未校准"
    assert rec["voided_at"]  # 操作时间已留存
    # 前一版本随作废记录留存
    assert rec["previous"] == {
        "revision": 1,
        "measured_at": "2026-10-01T00:10:00Z",
        "temperature": 23.0,
    }
    # 当前审定剔除该读数并立即重算：超限段消失，仅剩未知（间隔超限）
    ev = body["current_evaluation"]
    assert ev["reading_count"] == 2
    assert ev["verdict"] == "pending"
    assert ev["max_continuous_exceeded_seconds"] == 0.0
    assert not ev["exceeded_segments"]

    # 活动读数列表不再包含 b
    assert set(active_map(client, 1)) == {"a", "c"}
    # 与响应内重算一致
    assert client.get("/api/v1/windows/1/evaluation").json() == ev


def test_void_persists_revision_with_reason_time_and_previous(client):
    seed_window(client)
    void(client, 1, "b", 2, "误采：重复上报")
    rows = history_rows(1, "b")
    assert [(r["revision"], r["status"]) for r in rows] == [(1, "superseded"), (2, "voided")]
    assert rows[1]["void_reason"] == "误采：重复上报"
    assert rows[1]["prev_revision"] == 1

    # 修订历史端点呈现作废记录（原因/时间/前一版本）
    revs = client.get("/api/v1/windows/1/readings/b/revisions").json()
    assert [r["revision"] for r in revs] == [1, 2]
    assert revs[1]["status"] == "voided"
    assert revs[1]["void_reason"] == "误采：重复上报"
    assert revs[1]["voided_at"]
    assert revs[1]["prev_revision"] == 1
    assert revs[1]["measured_at"] == "2026-10-01T00:10:00Z"  # 前一版本内容
    assert revs[1]["temperature"] == 23.0


def test_void_replay_same_revision_and_reason_idempotent(client):
    seed_window(client)
    r1 = void(client, 1, "b", 2, "误采")
    assert r1.status_code == 201 and r1.json()["effect"] == "voided"
    r2 = void(client, 1, "b", 2, "误采")
    assert r2.status_code == 201 and r2.json()["effect"] == "idempotent"
    # 重放返回同一作废记录（操作时间不变），且不产生新修订行
    assert r2.json()["void"] == r1.json()["void"]
    assert len(history_rows(1, "b")) == 2


def test_void_same_revision_different_reason_409(client):
    seed_window(client)
    void(client, 1, "b", 2, "误采")
    r = void(client, 1, "b", 2, "另一个原因")
    assert r.status_code == 409 and "原因冲突" in r.json()["detail"]
    # 状态不变
    assert set(active_map(client, 1)) == {"a", "c"}
    assert len(history_rows(1, "b")) == 2


def test_void_lower_or_equal_revision_409(client):
    seed_window(client)
    # 低于当前修订号
    r = void(client, 1, "b", 0, "误采")
    assert r.status_code == 422  # revision 必须 >= 1（请求校验）
    r = client.post("/api/v1/windows/1/readings/b/void",
                    json={"revision": 1, "reason": "误采"})
    assert r.status_code == 409 and "高于当前修订号" in r.json()["detail"]
    # 作废后：低于作废修订号 -> 409
    void(client, 1, "b", 2, "误采")
    r = void(client, 1, "b", 1, "误采")
    assert r.status_code == 409 and "旧修订号" in r.json()["detail"]


def test_void_never_existed_reading_404(client):
    seed_window(client)
    r = void(client, 1, "ghost", 1, "误采")
    assert r.status_code == 404 and "从未存在" in r.json()["detail"]


def test_void_missing_window_404(client):
    seed_window(client)
    r = void(client, 999, "b", 2, "误采")
    assert r.status_code == 404


def test_void_already_voided_with_new_revision_409(client):
    seed_window(client)
    void(client, 1, "b", 2, "误采")
    r = void(client, 1, "b", 3, "再次作废")
    assert r.status_code == 409 and "不能再次" in r.json()["detail"]
    # 作废记录不变
    rows = history_rows(1, "b")
    assert [(row["revision"], row["status"]) for row in rows] == [(1, "superseded"), (2, "voided")]


def test_void_blank_reason_rejected(client):
    seed_window(client)
    for bad in ("", "   ", "\n\t"):
        r = void(client, 1, "b", 2, bad)
        assert r.status_code == 422, bad
    # 缺 reason 字段同样 422
    r = client.post("/api/v1/windows/1/readings/b/void", json={"revision": 2})
    assert r.status_code == 422
    assert set(active_map(client, 1)) == {"a", "b", "c"}


def test_void_frees_timestamp_for_other_reading(client):
    seed_window(client)
    void(client, 1, "b", 2, "误采")
    # 作废腾出的时刻可被其它读数使用
    r = submit_one(client, 1, reading("d", 1, 10, 19.5))
    assert r.status_code == 201 and r.json()["effect"] == "created"


# ---------------------------------------------------------------- 恢复（单条）

def test_restore_with_higher_revision_single(client):
    seed_window(client)
    void(client, 1, "b", 2, "误采")
    assert set(active_map(client, 1)) == {"a", "c"}

    r = submit_one(client, 1, reading("b", 3, 10, 21.0))
    assert r.status_code == 201, r.text
    assert r.json()["effect"] == "restored"
    assert r.json()["reading"]["revision"] == 3
    assert r.json()["current_evaluation"]["reading_count"] == 3

    rows = active_map(client, 1)
    assert rows["b"]["revision"] == 3 and rows["b"]["temperature"] == 21.0
    # 历史：rev1 superseded、rev2 voided（保留）、rev3 active
    hist = history_rows(1, "b")
    assert [(row["revision"], row["status"]) for row in hist] == \
        [(1, "superseded"), (2, "voided"), (3, "active")]


def test_restore_obeys_window_boundary(client):
    seed_window(client)
    void(client, 1, "b", 2, "误采")
    r = submit_one(client, 1, reading("b", 3, 61, 20.0))  # 窗口外
    assert r.status_code == 422 and "超出窗口" in r.json()["detail"]
    assert set(active_map(client, 1)) == {"a", "c"}  # 未复活


def test_restore_obeys_same_timestamp_uniqueness(client):
    seed_window(client)
    void(client, 1, "b", 2, "误采")
    # 恢复到 c 的时刻 -> 422
    r = submit_one(client, 1, reading("b", 3, 20, 20.0))
    assert r.status_code == 422 and "同刻" in r.json()["detail"]
    assert set(active_map(client, 1)) == {"a", "c"}


def test_old_revision_cannot_resurrect(client):
    seed_window(client)
    void(client, 1, "b", 2, "误采")
    # 重放作废前的 rev1（内容与历史一致）-> 409，不得复活
    r = submit_one(client, 1, reading("b", 1, 10, 23.0))
    assert r.status_code == 409 and "旧修订号" in r.json()["detail"]
    assert set(active_map(client, 1)) == {"a", "c"}
    # 与作废修订号相同的提交 -> 409（修订号已被作废记录占用）
    r = submit_one(client, 1, reading("b", 2, 10, 23.0))
    assert r.status_code == 409 and "作废" in r.json()["detail"]
    assert set(active_map(client, 1)) == {"a", "c"}
    # 历史不产生新行
    assert len(history_rows(1, "b")) == 2


def test_restore_then_void_again_with_higher_revision(client):
    seed_window(client)
    void(client, 1, "b", 2, "第一次误采")
    submit_one(client, 1, reading("b", 3, 10, 21.0))
    # 恢复后可再次以更高修订号作废
    r = void(client, 1, "b", 4, "复核后仍为误采")
    assert r.status_code == 201 and r.json()["effect"] == "voided"
    assert r.json()["void"]["previous"]["revision"] == 3
    assert set(active_map(client, 1)) == {"a", "c"}
    # 第一次的作废记录重放 -> 旧修订号 409
    r = void(client, 1, "b", 2, "第一次误采")
    assert r.status_code == 409


# ---------------------------------------------------------------- 恢复（批量）

def test_restore_via_batch(client):
    seed_window(client)
    submit_one(client, 1, reading("d", 1, 30, 22.5))
    void(client, 1, "b", 2, "误采-b")
    void(client, 1, "d", 2, "误采-d")
    assert set(active_map(client, 1)) == {"a", "c"}

    r = submit_batch(client, 1, [
        reading("b", 3, 10, 21.0),
        reading("d", 3, 30, 20.5),
    ])
    assert r.status_code == 201, r.text
    assert [x["effect"] for x in r.json()["results"]] == ["restored", "restored"]
    assert r.json()["current_evaluation"]["reading_count"] == 4
    rows = active_map(client, 1)
    assert rows["b"]["revision"] == 3 and rows["d"]["revision"] == 3


def test_batch_restore_conflict_rolls_back_whole_batch(client):
    seed_window(client)
    void(client, 1, "b", 2, "误采")
    before_hist = history_rows(1, "b")

    # 批内第二项与既有读数 c 同刻 -> 整批回滚，b 不得恢复
    r = submit_batch(client, 1, [
        reading("b", 3, 10, 21.0),
        reading("x", 1, 20, 20.0),
    ])
    assert r.status_code == 422 and "同刻" in r.json()["detail"]
    assert set(active_map(client, 1)) == {"a", "c"}
    assert history_rows(1, "b") == before_hist  # 无 rev3

    # 批内第二项旧修订号 -> 409，整批回滚
    r = submit_batch(client, 1, [
        reading("b", 3, 10, 21.0),
        reading("a", 1, 0, 99.0),  # 同 ID 同修订号内容冲突
    ])
    assert r.status_code == 409
    assert set(active_map(client, 1)) == {"a", "c"}
    assert history_rows(1, "b") == before_hist


def test_batch_restore_final_state_same_timestamp_rejected(client):
    seed_window(client)
    submit_one(client, 1, reading("d", 1, 30, 20.0))
    void(client, 1, "b", 2, "误采-b")
    void(client, 1, "d", 2, "误采-d")
    # 两条作废读数恢复到同一时刻 -> 最终状态同刻，整批拒收
    r = submit_batch(client, 1, [
        reading("b", 3, 15, 20.0),
        reading("d", 3, 15, 21.0),
    ])
    assert r.status_code == 422 and "同刻" in r.json()["detail"]
    assert set(active_map(client, 1)) == {"a", "c"}


def test_batch_old_revision_cannot_resurrect_and_rolls_back(client):
    seed_window(client)
    void(client, 1, "b", 2, "误采")
    r = submit_batch(client, 1, [
        reading("b", 1, 10, 23.0),   # 旧版提交不得复活 -> 409
        reading("x", 1, 40, 20.0),   # 本应 created，随批回滚
    ])
    assert r.status_code == 409 and "旧修订号" in r.json()["detail"]
    assert set(active_map(client, 1)) == {"a", "c"}


# ---------------------------------------------------------------- 证书与修订影响报告

def test_certificate_snapshots_and_revision_report_reflect_void_and_restore(client):
    seed_window(client)
    c1 = client.post("/api/v1/windows/1/certificates/issue", json={"note": "v1"}).json()
    assert [r["reading_id"] for r in c1["snapshot"]["readings"]] == ["a", "b", "c"]

    # 作废 b 后签发 v2：快照剔除 b；v1 快照保持原样
    void(client, 1, "b", 2, "误采")
    c2 = client.post("/api/v1/windows/1/certificates/issue", json={"note": "v2"}).json()
    assert c2["version"] == 2
    assert [r["reading_id"] for r in c2["snapshot"]["readings"]] == ["a", "c"]
    old = client.get(f"/api/v1/certificates/{c1['id']}").json()
    assert [r["reading_id"] for r in old["snapshot"]["readings"]] == ["a", "b", "c"]

    # 修订影响报告 v1->v2：b 计入 removed
    rep = client.get("/api/v1/certificates/revision-report"
                     f"?old_certificate_id={c1['id']}&new_certificate_id={c2['id']}").json()
    diff = rep["readings_diff"]
    assert diff["removed_count"] == 1 and diff["removed"][0]["reading_id"] == "b"
    assert diff["added_count"] == 0 and diff["changed_count"] == 0

    # 恢复 b 后签发 v3：报告 v2->v3 呈现 b 再出现（added）
    submit_one(client, 1, reading("b", 3, 10, 21.0))
    c3 = client.post("/api/v1/windows/1/certificates/issue", json={"note": "v3"}).json()
    assert [r["reading_id"] for r in c3["snapshot"]["readings"]] == ["a", "b", "c"]
    rep2 = client.get("/api/v1/certificates/revision-report"
                      f"?old_certificate_id={c2['id']}&new_certificate_id={c3['id']}").json()
    diff2 = rep2["readings_diff"]
    assert diff2["added_count"] == 1 and diff2["added"][0]["reading_id"] == "b"
    assert diff2["added"][0]["revision"] == 3
    assert diff2["removed_count"] == 0
    # v2 快照仍保持作废后状态
    mid = client.get(f"/api/v1/certificates/{c2['id']}").json()
    assert [r["reading_id"] for r in mid["snapshot"]["readings"]] == ["a", "c"]


def test_void_and_restore_change_verdict_across_certificates(client):
    seed_window(client)
    c1 = client.post("/api/v1/windows/1/certificates/issue", json={}).json()
    assert c1["verdict"] == "fail"
    void(client, 1, "b", 2, "误采")
    c2 = client.post("/api/v1/windows/1/certificates/issue", json={}).json()
    assert c2["verdict"] == "pending"  # 超限源被剔除
    rep = client.get("/api/v1/certificates/revision-report"
                     f"?old_certificate_id={c1['id']}&new_certificate_id={c2['id']}").json()
    assert rep["summary"]["verdict_before"] == "fail"
    assert rep["summary"]["verdict_after"] == "pending"
    assert rep["summary"]["verdict_direction"] == "改善"


# ---------------------------------------------------------------- 修订历史端点

def test_revisions_endpoint_404_for_unknown_reading(client):
    seed_window(client)
    assert client.get("/api/v1/windows/1/readings/ghost/revisions").status_code == 404
    assert client.get("/api/v1/windows/999/readings/a/revisions").status_code == 404


def test_revisions_endpoint_lists_full_lifecycle(client):
    seed_window(client)
    submit_one(client, 1, reading("b", 2, 10, 23.5))   # updated
    void(client, 1, "b", 3, "误采")                     # voided
    submit_one(client, 1, reading("b", 4, 10, 21.0))   # restored
    revs = client.get("/api/v1/windows/1/readings/b/revisions").json()
    assert [(r["revision"], r["status"]) for r in revs] == \
        [(1, "superseded"), (2, "superseded"), (3, "voided"), (4, "active")]
    assert revs[2]["void_reason"] == "误采" and revs[2]["prev_revision"] == 2
    assert revs[3]["void_reason"] is None
