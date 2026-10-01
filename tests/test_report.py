"""修订影响报告纯函数测试（标准库 + pytest，无 IO）。

运行：PYTHONPATH=. python -m pytest tests/test_report.py -q
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.evaluation import evaluate
from app.models import VERDICT_LABELS_ZH
from app.report import (
    BOUNDARY_EMPTY,
    UNKNOWN_BOUNDARY_GAP,
    UNKNOWN_SAMPLE_GAP,
    build_evidence_segments,
    build_revision_report,
    diff_readings,
    merge_adjacent_changes,
)
from app.timeutils import parse_stored

WS = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
WE = WS + timedelta(hours=1)


def iso(minute: float, second: float = 0) -> str:
    return (WS + timedelta(minutes=minute, seconds=second)).isoformat().replace("+00:00", "Z")


def make_snapshot(readings, *, verdict=None, tmin=18.0, tmax=22.0,
                  max_exceed=300.0, max_gap=120.0, window_id=1):
    """构造与签发结构一致的快照；evaluation 用同一算法实算。"""
    parsed = [
        (parse_stored(r["measured_at"]), r["temperature"]) for r in readings
    ]
    ev = evaluate(WS, WE, tmin, tmax, max_exceed, max_gap, parsed)
    if verdict is not None:
        ev["verdict"] = verdict
    return {
        "window": {
            "id": window_id,
            "name": "case",
            "start_at": iso(0),
            "end_at": iso(60),
            "allowed_temp_min": tmin,
            "allowed_temp_max": tmax,
            "max_continuous_exceed_seconds": max_exceed,
            "max_sample_interval_seconds": max_gap,
        },
        "evaluation": ev,
        "readings": list(readings),
    }


def r(rid, rev, minute, temp, *, second=0):
    return {
        "reading_id": rid, "revision": rev,
        "measured_at": iso(minute, second), "temperature": temp,
        "status": "active", "created_at": iso(0),
    }


def make_cert(cid, version, snap, verdict=None):
    v = verdict or snap["evaluation"]["verdict"]
    return {
        "id": cid, "window_id": snap["window"]["id"], "version": version,
        "verdict": v, "verdict_label_zh": VERDICT_LABELS_ZH[v],
        "issued_at": iso(0), "note": None, "snapshot": snap,
    }


def statuses(segs):
    return [(s.start, s.end, s.status) for s in segs]


# ------------------------------------------------------------ 证据区段

def test_evidence_segments_match_evaluation_partition():
    """带证据区段的状态分区必须与审定算法完全一致（边界、状态）。"""
    readings = [r("a", 1, 0, 20.0), r("b", 1, 1, 23.0),
                r("c", 1, 6, 20.0), r("d", 1, 20, 20.0)]
    snap = make_snapshot(readings, max_gap=300.0)
    segs = build_evidence_segments(snap)
    # 相邻原始区段首尾相接覆盖整窗
    assert segs[0].start == WS and segs[-1].end == WE
    for x, y in zip(segs, segs[1:]):
        assert x.end == y.start
    got = [(s.status, (s.end - s.start).total_seconds()) for s in segs]
    # [0,1) normal(a)；[1,6) exceeded(b) 间隔 300s 恰好不超限；
    # [6,20) sample_gap(c->d, 840>300)；[20,60) boundary trailing(d)
    assert got == [("normal", 60.0), ("exceeded", 300.0),
                   ("unknown", 840.0), ("unknown", 2400.0)]
    assert segs[2].unknown_reason == UNKNOWN_SAMPLE_GAP
    assert (segs[2].left_reading_id, segs[2].left_revision) == ("c", 1)
    assert (segs[2].right_reading_id, segs[2].right_revision) == ("d", 1)
    assert segs[3].unknown_reason == UNKNOWN_BOUNDARY_GAP
    assert segs[3].boundary_side == "trailing"
    assert (segs[3].left_reading_id, segs[3].left_revision) == ("d", 1)
    assert segs[0].reading_id == "a" and segs[0].revision == 1
    assert segs[1].reading_id == "b" and segs[1].revision == 1


def test_empty_snapshot_is_boundary_gap_empty():
    snap = make_snapshot([])
    segs = build_evidence_segments(snap)
    assert len(segs) == 1
    assert segs[0].status == "unknown"
    assert segs[0].unknown_reason == UNKNOWN_BOUNDARY_GAP
    assert segs[0].boundary_side == BOUNDARY_EMPTY
    ev = segs[0].evidence()
    assert ev == {"unknown_reason": UNKNOWN_BOUNDARY_GAP, "boundary_side": BOUNDARY_EMPTY}


def test_leading_boundary_gap_points_to_first_reading():
    snap = make_snapshot([r("a", 1, 5, 20.0), r("b", 1, 6, 20.0)], max_gap=600.0)
    segs = build_evidence_segments(snap)
    lead = segs[0]
    assert lead.status == "unknown"
    assert lead.unknown_reason == UNKNOWN_BOUNDARY_GAP
    assert lead.boundary_side == "leading"
    assert lead.evidence() == {
        "unknown_reason": UNKNOWN_BOUNDARY_GAP,
        "boundary_side": "leading",
        "right_reading_id": "a", "right_revision": 1,
    }


def test_gap_equal_limit_is_not_unknown():
    # 间隔恰好 120s：正常区段，证据为左侧读数
    snap = make_snapshot([r("a", 1, 0, 20.0), r("b", 1, 2, 20.0)], max_gap=120.0)
    segs = [s for s in build_evidence_segments(snap) if s.start < parse_stored(iso(2))]
    assert segs[-1].status == "normal"
    assert segs[-1].unknown_reason is None
    assert segs[-1].evidence() == {"reading_id": "a", "revision": 1}


# ------------------------------------------------------------ 读数差异

def test_diff_readings_added_changed_removed():
    old = make_snapshot([
        r("same", 1, 0, 20.0),
        r("temp", 1, 1, 20.0),
        r("time", 1, 2, 20.0),
        r("gone", 1, 3, 20.0),
    ])
    new = make_snapshot([
        r("same", 1, 0, 20.0),
        r("temp", 2, 1, 23.5),
        r("time", 1, 4, 20.0),
        r("new", 1, 5, 20.0),
    ])
    d = diff_readings(old, new)
    assert [x["reading_id"] for x in d["added"]] == ["new"]
    assert [x["reading_id"] for x in d["removed"]] == ["gone"]
    ch = {x["reading_id"]: x for x in d["changed"]}
    assert set(ch) == {"temp", "time"}
    assert ch["temp"]["changed_fields"] == ["revision", "temperature"]
    assert ch["temp"]["before"]["temperature"] == 20.0
    assert ch["temp"]["after"]["revision"] == 2 and ch["temp"]["after"]["temperature"] == 23.5
    assert ch["time"]["changed_fields"] == ["measured_at"]
    assert (d["added_count"], d["removed_count"], d["changed_count"]) == (1, 1, 2)


def test_diff_equivalent_offset_instant_not_changed():
    offset_iso = "2026-10-01T08:00:00+08:00"  # 与 00:00:00Z 同一时刻
    old = make_snapshot([r("a", 1, 0, 20.0)])
    new = make_snapshot([{**r("a", 1, 0, 20.0), "measured_at": offset_iso}])
    d = diff_readings(old, new)
    assert d["changed"] == [] and d["added"] == [] and d["removed"] == []


# ------------------------------------------------------------ 时间线对齐

def _changes(old_readings, new_readings, **kw):
    old = make_snapshot(old_readings, **kw)
    new = make_snapshot(new_readings, **kw)
    return build_revision_report(
        make_cert(1, 1, old), make_cert(2, 2, new)
    )["timeline"]["changes"]


def test_sample_gap_split_into_changes_keeps_gap_evidence():
    changes = _changes(
        [r("a", 1, 0, 20.0), r("x", 1, 9, 20.0)],
        [r("a", 1, 0, 20.0), r("b", 1, 2, 20.0), r("c", 1, 4, 20.0),
         r("d", 1, 6, 20.0), r("e", 1, 8, 20.0), r("x", 1, 9, 20.0)],
        max_gap=120.0,
    )
    # [0,2)[2,4)[4,6)[6,8)[8,9) 五段 sample_gap->normal；
    # before 证据同为 (a,x) 间隔，但 after 读数不同 -> 不合并
    assert len(changes) == 5
    assert all(c["before"]["status"] == "unknown" for c in changes)
    assert all(c["after"]["status"] == "normal" for c in changes)
    for c in changes:
        ev = c["before"]["evidence"]
        assert ev["unknown_reason"] == UNKNOWN_SAMPLE_GAP
        assert (ev["left_reading_id"], ev["left_revision"]) == ("a", 1)
        assert (ev["right_reading_id"], ev["right_revision"]) == ("x", 1)
    after_ids = [c["after"]["evidence"]["reading_id"] for c in changes]
    assert after_ids == ["a", "b", "c", "d", "e"]
    # [9,10) 在旧版为 trailing 未知、新版仍为 trailing(x) 未知 -> 不在输出
    assert all(c["end"] <= iso(9) for c in changes)


def test_empty_to_covered_change_whole_window():
    changes = _changes(
        [],
        [r("a", 1, 0, 20.0), r("b", 1, 30, 20.0), r("c", 1, 60, 20.0)],
        max_gap=6000.0,
    )
    # 末读在窗口终点只终止覆盖：仅 [0,30)a 与 [30,60)b 两段
    assert len(changes) == 2
    for c in changes:
        assert c["before"]["status"] == "unknown"
        assert c["before"]["evidence"] == {
            "unknown_reason": UNKNOWN_BOUNDARY_GAP,
            "boundary_side": BOUNDARY_EMPTY,
        }
    assert changes[0]["start"] == iso(0) and changes[0]["end"] == iso(30)
    assert changes[1]["start"] == iso(30) and changes[1]["end"] == iso(60)
    assert changes[1]["after"]["evidence"] == {"reading_id": "b", "revision": 1}


def test_exceeded_to_normal_with_reading_revision_evidence():
    changes = _changes(
        [r("a", 1, 0, 20.0), r("b", 1, 5, 23.0), r("c", 1, 10, 20.0)],
        [r("a", 1, 0, 20.0), r("b", 2, 5, 20.0), r("c", 1, 10, 20.0)],
        max_gap=600.0,
    )
    assert len(changes) == 1
    ch = changes[0]
    assert ch["start"] == iso(5) and ch["end"] == iso(10)
    assert ch["before"] == {"status": "exceeded",
                            "evidence": {"reading_id": "b", "revision": 1}}
    assert ch["after"] == {"status": "normal",
                           "evidence": {"reading_id": "b", "revision": 2}}
    assert ch["duration_seconds"] == 300.0


def test_leading_gap_filled_change_has_boundary_evidence():
    changes = _changes(
        [r("a", 1, 5, 20.0), r("b", 1, 10, 20.0)],
        [r("n", 1, 0, 20.0), r("a", 1, 5, 20.0), r("b", 1, 10, 20.0)],
        max_gap=6000.0,
    )
    assert len(changes) == 1
    ch = changes[0]
    assert ch["start"] == iso(0) and ch["end"] == iso(5)
    assert ch["before"]["evidence"] == {
        "unknown_reason": UNKNOWN_BOUNDARY_GAP,
        "boundary_side": "leading",
        "right_reading_id": "a", "right_revision": 1,
    }
    assert ch["after"]["evidence"] == {"reading_id": "n", "revision": 1}


def test_no_status_change_returns_empty_timeline():
    # 温度修订但两版都超限（23 -> 24）：时间线状态无变化
    changes = _changes(
        [r("a", 1, 0, 20.0), r("b", 1, 5, 23.0), r("c", 1, 10, 20.0)],
        [r("a", 1, 0, 20.0), r("b", 2, 5, 24.0), r("c", 1, 10, 20.0)],
        max_gap=600.0,
    )
    assert changes == []


# ------------------------------------------------------------ 合并规则

def test_merge_adjacent_changes_helper():
    e_before = {"status": "unknown", "evidence": {"unknown_reason": "sample_gap"}}
    e_after_a = {"status": "normal", "evidence": {"reading_id": "a", "revision": 1}}
    e_after_b = {"status": "normal", "evidence": {"reading_id": "b", "revision": 1}}

    def ch(m0, m1, after):
        return {"start": iso(m0), "end": iso(m1),
                "duration_seconds": (m1 - m0) * 60.0,
                "before": dict(e_before), "after": dict(after)}

    # 相邻 + 两侧状态与证据完全相同 -> 合并
    merged = merge_adjacent_changes([ch(0, 2, e_after_a), ch(2, 5, e_after_a)])
    assert len(merged) == 1
    assert merged[0]["start"] == iso(0) and merged[0]["end"] == iso(5)
    assert merged[0]["duration_seconds"] == 300.0

    # 相邻但 after 证据不同 -> 不合并
    split = merge_adjacent_changes([ch(0, 2, e_after_a), ch(2, 5, e_after_b)])
    assert len(split) == 2

    # 被“无变化”区间隔开（首尾不接）-> 不合并
    gapped = merge_adjacent_changes([ch(0, 2, e_after_a), ch(3, 5, e_after_a)])
    assert len(gapped) == 2


# ------------------------------------------------------------ 汇总

def test_summary_fail_to_pass():
    old = make_snapshot(
        [r("a", 1, 0, 20.0), r("b", 1, 1, 23.0), r("c", 1, 7, 24.0),
         r("d", 1, 8, 20.0), r("e", 1, 10, 20.0)],
        max_exceed=300.0, max_gap=600.0)
    new = make_snapshot(
        [r("a", 1, 0, 20.0), r("b", 2, 1, 20.0), r("c", 2, 7, 20.0),
         r("d", 1, 8, 20.0), r("e", 1, 10, 20.0)],
        max_exceed=300.0, max_gap=600.0)
    # 60 分钟窗口改为 10 分钟窗口以匹配读数
    for snap in (old, new):
        snap["window"]["end_at"] = iso(10)
        snap["evaluation"] = evaluate(
            WS, WS + timedelta(minutes=10), 18.0, 22.0, 300.0, 600.0,
            [(parse_stored(x["measured_at"]), x["temperature"])
             for x in snap["readings"]])
    report = build_revision_report(make_cert(1, 1, old), make_cert(2, 2, new))
    s = report["summary"]
    assert s["verdict_before"] == "fail" and s["verdict_after"] == "pass"
    assert s["verdict_direction"] == "改善" and s["verdict_changed"] is True
    assert s["max_continuous_exceeded_seconds_before"] == 420.0
    assert s["max_continuous_exceeded_seconds_after"] == 0.0
    assert s["max_continuous_exceeded_seconds_delta"] == -420.0
    assert s["timeline_change_count"] == 2
    assert "不合格" in s["conclusion_zh"] and "合格" in s["conclusion_zh"]
    # 读数差异：b、c 两条变更（revision+temperature）
    diff = report["readings_diff"]
    assert diff["changed_count"] == 2 and diff["added_count"] == 0


def test_summary_pass_to_fail_direction_worsens():
    # 旧版：首读起点、末读终点、间隔合法、温度正常 -> pass
    old = make_snapshot([r("a", 1, 0, 20.0), r("b", 1, 30, 20.0),
                         r("z", 1, 60, 20.0)],
                        max_exceed=300.0, max_gap=6000.0)
    assert old["evaluation"]["verdict"] == "pass"
    # 新版：a 修订为超温，覆盖 [0,30) 共 1800s > 300 限值 -> fail
    new = make_snapshot([r("a", 2, 0, 24.0), r("b", 1, 30, 20.0),
                         r("z", 1, 60, 20.0)],
                        max_exceed=300.0, max_gap=6000.0)
    assert new["evaluation"]["verdict"] == "fail"
    report = build_revision_report(make_cert(1, 1, old), make_cert(2, 2, new))
    s = report["summary"]
    assert s["verdict_before"] == "pass"
    assert s["verdict_after"] == "fail"
    assert s["verdict_direction"] == "恶化"
    assert s["max_continuous_exceeded_seconds_before"] == 0.0
    assert s["max_continuous_exceeded_seconds_after"] == 1800.0
    assert s["max_continuous_exceeded_seconds_delta"] == 1800.0


def test_report_carries_certificate_refs_and_window():
    old = make_snapshot([r("a", 1, 0, 20.0)], max_gap=6000.0)
    new = make_snapshot([r("a", 2, 0, 20.0)], max_gap=6000.0)
    c1, c2 = make_cert(11, 1, old), make_cert(12, 2, new)
    c2["note"] = "重签"
    report = build_revision_report(c1, c2)
    assert report["old_certificate"]["id"] == 11
    assert report["new_certificate"]["id"] == 12
    assert report["new_certificate"]["note"] == "重签"
    assert report["window"]["id"] == 1
