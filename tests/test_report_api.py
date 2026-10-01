"""修订影响报告 HTTP API 集成测试。

运行：PYTHONPATH=. python -m pytest tests/test_report_api.py -q
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
    from app.main import app

    with TestClient(app) as c:
        yield c


def iso(minute: float, second: float = 0) -> str:
    t = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc) + timedelta(
        minutes=minute, seconds=second)
    return t.isoformat().replace("+00:00", "Z")


def make_window(client, minutes=10, max_exceed=300.0, max_gap=600.0, name="case"):
    body = {
        "name": name,
        "start_at": iso(0),
        "end_at": iso(minutes),
        "allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
        "max_continuous_exceed_seconds": max_exceed,
        "max_sample_interval_seconds": max_gap,
    }
    r = client.post("/api/v1/windows", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


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


def report(client, old_id, new_id):
    return client.get("/api/v1/certificates/revision-report",
                      params={"old_certificate_id": old_id,
                              "new_certificate_id": new_id})


# ------------------------------------------------------------ 主流程

def test_revision_report_fail_to_pass_full(client):
    wid = make_window(client, minutes=10, max_gap=600.0)
    # 00:01->00:08 连续超限 420s > 300 -> fail
    for rid, m, t in [("a", 0, 20.0), ("b", 1, 23.0), ("c", 7, 24.0),
                      ("d", 8, 20.0), ("e", 10, 20.0)]:
        submit(client, wid, rid, 1, m, t)
    c1 = issue(client, wid, "首签-不合格")
    assert c1["verdict"] == "fail"

    # 修订超温读数为正常后重签
    submit(client, wid, "b", 2, 1, 20.0)
    submit(client, wid, "c", 2, 7, 20.0)
    c2 = issue(client, wid, "整改后重签")
    assert c2["version"] == 2 and c2["verdict"] == "pass"

    r = report(client, c1["id"], c2["id"])
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["old_certificate"]["id"] == c1["id"]
    assert rep["new_certificate"]["id"] == c2["id"]
    assert rep["window"]["id"] == wid

    # 读数差异：b、c 两条 revision+temperature 变更，无新增/移除
    diff = rep["readings_diff"]
    assert diff["added"] == [] and diff["removed"] == []
    assert {x["reading_id"] for x in diff["changed"]} == {"b", "c"}
    for ch in diff["changed"]:
        assert ch["changed_fields"] == ["revision", "temperature"]

    # 时间线：[1,7) exceeded->normal 与 [7,8) exceeded->normal 两个半开区间
    changes = rep["timeline"]["changes"]
    assert [c["start"] for c in changes] == [iso(1), iso(7)]
    assert all(c["before"]["status"] == "exceeded" for c in changes)
    assert all(c["after"]["status"] == "normal" for c in changes)
    # 各侧对应读数 ID 与修订号
    assert changes[0]["before"]["evidence"] == {"reading_id": "b", "revision": 1}
    assert changes[0]["after"]["evidence"] == {"reading_id": "b", "revision": 2}
    assert changes[1]["before"]["evidence"] == {"reading_id": "c", "revision": 1}
    assert changes[1]["after"]["evidence"] == {"reading_id": "c", "revision": 2}
    assert rep["timeline"]["change_count"] == 2

    # 汇总
    s = rep["summary"]
    assert s["verdict_before"] == "fail" and s["verdict_after"] == "pass"
    assert s["verdict_direction"] == "改善"
    assert s["max_continuous_exceeded_seconds_before"] == 420.0
    assert s["max_continuous_exceeded_seconds_after"] == 0.0
    assert s["max_continuous_exceeded_seconds_delta"] == -420.0
    assert "不合格" in s["conclusion_zh"] and "420" in s["conclusion_zh"]


def test_report_unknown_boundary_vs_sample_gap(client):
    # 旧版：仅 00:03 一条读数 -> leading[0,3) boundary、trailing[3,10) boundary
    # 新版 max_gap=200：00:00->00:03(180s) 合法，但 00:03->00:08(300s) 超限
    wid = make_window(client, minutes=10, max_gap=200.0)
    submit(client, wid, "a", 1, 3, 20.0)
    c1 = issue(client, wid)

    # 新版：00:00 补首读，00:03 后隔 5 分钟才有读数 -> [3,8) 采样间隔超限；
    # 00:08->00:10 为 b 覆盖的正常区段（00:10 终止读数使其无尾部空白）
    submit(client, wid, "z", 1, 0, 20.0)
    submit(client, wid, "b", 1, 8, 20.0)
    submit(client, wid, "t", 1, 10, 20.0)
    c2 = issue(client, wid)

    rep = report(client, c1["id"], c2["id"]).json()
    changes = {(c["start"], c["end"]): c for c in rep["timeline"]["changes"]}

    # [0,3): leading 边界空白（右证据 a） -> normal(z)
    c03 = changes[(iso(0), iso(3))]
    assert c03["before"]["evidence"]["unknown_reason"] == "boundary_gap"
    assert c03["before"]["evidence"]["boundary_side"] == "leading"
    assert c03["before"]["evidence"]["right_reading_id"] == "a"
    assert c03["after"]["evidence"] == {"reading_id": "z", "revision": 1}

    # [3,8): 旧 trailing 边界空白 -> 新采样间隔超限；状态同为未知故**不输出**，
    # 但未知原因不同这一事实已由两侧证据在相邻变化区间外不体现——这里专门校验
    # 该区间确实缺席（仅状态变化才进入报告）。
    assert (iso(3), iso(8)) not in changes

    # [8,10): 旧 trailing 边界空白（左证据 a） -> normal(b)
    c810 = changes[(iso(8), iso(10))]
    assert c810["before"]["evidence"]["unknown_reason"] == "boundary_gap"
    assert c810["before"]["evidence"]["boundary_side"] == "trailing"
    assert c810["before"]["evidence"]["left_reading_id"] == "a"
    assert c810["after"]["evidence"] == {"reading_id": "b", "revision": 1}

    # 另直接核对新快照重建出的 [3,8) 为 sample_gap（a->b，间隔 300s>200s）
    # ——通过再对比一个“该区间变为正常”的版本不便构造，这里以单元测试已覆盖
    # sample_gap 证据为准（见 tests/test_report.py）。
    assert rep["timeline"]["change_count"] == 2


def test_report_empty_window_unknown_is_empty_boundary(client):
    wid = make_window(client, minutes=5, max_gap=600.0)
    c1 = issue(client, wid)  # 无任何读数
    submit(client, wid, "a", 1, 0, 20.0)
    submit(client, wid, "b", 1, 5, 20.0)
    c2 = issue(client, wid)
    rep = report(client, c1["id"], c2["id"]).json()
    changes = rep["timeline"]["changes"]
    # 末读恰在窗口终点只终止覆盖（自身时长 0），整个 [0,5) 为一个变化区间
    assert len(changes) == 1
    ch = changes[0]
    assert ch["start"] == iso(0) and ch["end"] == iso(5)
    assert ch["before"]["status"] == "unknown"
    assert ch["before"]["evidence"] == {
        "unknown_reason": "boundary_gap", "boundary_side": "empty"}
    assert ch["after"] == {"status": "normal",
                           "evidence": {"reading_id": "a", "revision": 1}}
    assert rep["readings_diff"]["added_count"] == 2
    assert rep["summary"]["verdict_before"] == "pending"
    assert rep["summary"]["verdict_after"] == "pass"


def test_report_only_snapshot_not_active_readings(client):
    """报告只依据证书快照：签发后再修订活动读数，报告内容不随之改变。"""
    wid = make_window(client, minutes=5, max_gap=6000.0)
    submit(client, wid, "a", 1, 0, 23.0)   # 超温
    submit(client, wid, "b", 1, 2, 20.0)
    c1 = issue(client, wid, "v1")
    submit(client, wid, "a", 2, 0, 20.0)
    c2 = issue(client, wid, "v2")

    rep1 = report(client, c1["id"], c2["id"]).json()
    # 签发之后继续把活动读数改成另一个值（rev3），不产生新证书
    submit(client, wid, "a", 3, 0, 100.0)
    rep2 = report(client, c1["id"], c2["id"]).json()
    assert rep1 == rep2
    # 两张证书快照本身也未被活动读数影响
    assert client.get(f"/api/v1/certificates/{c2['id']}").json()[
        "snapshot"]["readings"][0]["revision"] == 2


def test_report_new_reading_added(client):
    wid = make_window(client, minutes=5, max_gap=6000.0)
    submit(client, wid, "a", 1, 0, 20.0)
    submit(client, wid, "z", 1, 5, 20.0)
    c1 = issue(client, wid)
    submit(client, wid, "b", 1, 2, 20.0)  # 新增读数
    c2 = issue(client, wid)
    rep = report(client, c1["id"], c2["id"]).json()
    added = rep["readings_diff"]["added"]
    assert len(added) == 1 and added[0]["reading_id"] == "b"
    assert added[0]["revision"] == 1 and added[0]["temperature"] == 20.0
    # 新增读数改变了覆盖证据：[0,2) 旧证据 a -> [0,2) 新证据仍 a（状态不变）；
    # [2,5) 旧证据 a -> 新证据 b（状态仍 normal）；时间线状态应无变化
    assert rep["timeline"]["changes"] == []
    assert rep["summary"]["timeline_change_count"] == 0


# ------------------------------------------------------------ 拒绝路径

def test_report_certificate_not_found_404(client):
    wid = make_window(client)
    c = issue(client, wid)
    r = report(client, c["id"], 999)
    assert r.status_code == 404 and "证书 999 不存在" in r.json()["detail"]
    r = report(client, 998, c["id"])
    assert r.status_code == 404 and "证书 998 不存在" in r.json()["detail"]


def test_report_same_id_rejected_422(client):
    wid = make_window(client)
    c = issue(client, wid)
    r = report(client, c["id"], c["id"])
    assert r.status_code == 422 and "相同" in r.json()["detail"]


def test_report_cross_window_rejected_422(client):
    w1 = make_window(client, name="a")
    w2 = make_window(client, name="b")
    c1 = issue(client, w1)
    c2 = issue(client, w2)
    r = report(client, c1["id"], c2["id"])
    assert r.status_code == 422 and "同一审定窗口" in r.json()["detail"]


def test_report_version_order_rejected_422(client):
    wid = make_window(client)
    issue(client, wid)
    c2 = issue(client, wid)
    # 新版 ID 放在 old 位置（版本倒序）
    r = report(client, c2["id"], _first_cert_id(client, wid))
    assert r.status_code == 422 and "版本未按先后排列" in r.json()["detail"]


def _first_cert_id(client, wid):
    return client.get(f"/api/v1/windows/{wid}/certificates").json()[0]["id"]


def test_report_missing_query_param_422(client):
    wid = make_window(client)
    c = issue(client, wid)
    r = client.get("/api/v1/certificates/revision-report",
                   params={"old_certificate_id": c["id"]})
    assert r.status_code == 422
    r = client.get("/api/v1/certificates/revision-report",
                   params={"old_certificate_id": "abc",
                           "new_certificate_id": c["id"]})
    assert r.status_code == 422


def test_static_route_not_shadowed(client):
    """整数证书 ID 路由仍可正常解析，revision-report 静态路由优先。"""
    wid = make_window(client)
    c = issue(client, wid)
    r = client.get(f"/api/v1/certificates/{c['id']}")
    assert r.status_code == 200 and r.json()["id"] == c["id"]
    r = client.get("/api/v1/certificates/revision-report")
    assert r.status_code == 422  # 缺少查询参数，而非证书不存在
