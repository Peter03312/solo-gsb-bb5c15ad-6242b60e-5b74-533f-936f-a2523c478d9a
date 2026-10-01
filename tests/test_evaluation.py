"""审定算法单元测试（标准库即可运行）。

运行：python -m pytest tests/ -q（需安装 pytest）；
或：python tests/test_evaluation.py
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.evaluation import evaluate

WS = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
WE = WS + timedelta(hours=1)


def ts(minute: float, second: float = 0) -> datetime:
    return WS + timedelta(minutes=minute, seconds=second)


def seg_summary(result):
    out = []
    for key in ("normal_segments", "exceeded_segments", "unknown_segments"):
        for s in result[key]:
            out.append((s["start"][11:19], s["end"][11:19], s["status"][0]))
    return sorted(out)


def test_no_readings_whole_window_unknown():
    r = evaluate(WS, WE, 18, 22, 300, 120, [])
    assert r["verdict"] == "pending"
    assert r["unknown_segments"][0]["duration_seconds"] == 3600
    assert r["max_continuous_exceeded_seconds"] == 0


def test_leading_and_trailing_without_readings_unknown():
    # 00:01 与 00:02 有读数：前段 60s 未知、中间 60s 正常、尾段 3480s 未知
    r = evaluate(WS, WE, 18, 22, 300, 120,
                 [(ts(1), 20.0), (ts(2), 21.0)])
    assert r["verdict"] == "pending"
    assert r["normal_segments"][0]["duration_seconds"] == 60
    assert r["unknown_segments"][0]["start"][11:19] == "00:00:00"
    assert r["unknown_segments"][0]["duration_seconds"] == 60
    assert r["unknown_segments"][-1]["duration_seconds"] == 3480


def test_gap_over_limit_entire_interval_unknown():
    r = evaluate(WS, WE, 18, 22, 300, 120,
                 [(ts(0), 20.0), (ts(5), 20.0)])
    # [00:00,00:05) 间隔超限未知，与 [00:05,01:00) 尾部未知相邻，合并为整段
    assert len(r["unknown_segments"]) == 1
    assert r["unknown_segments"][0]["duration_seconds"] == 3600.0
    assert r["normal_segments"] == []


def test_gap_over_limit_kept_distinct_when_bounded_by_normal():
    # 两侧有正常区段夹着时，超限间隔单独成段，不与尾部未知合并
    r = evaluate(WS, WE, 18, 22, 300, 120,
                 [(ts(0), 20.0), (ts(1), 20.0), (ts(6), 20.0), (ts(7), 20.0)])
    durations = sorted(s["duration_seconds"] for s in r["unknown_segments"])
    assert durations == [300.0, 3180.0]
    assert r["normal_segments"][0]["duration_seconds"] == 60.0


def test_gap_equal_limit_is_allowed():
    # 间隔恰好 120s（不超上限），温度正常
    r = evaluate(WS, WE, 18, 22, 300, 120,
                 [(ts(0), 20.0), (ts(2), 20.0)])
    assert r["unknown_segments"][0]["start"][11:19] == "00:02:00"
    assert r["normal_segments"][0]["duration_seconds"] == 120


def test_exceeded_failure_and_max_continuous():
    r = evaluate(WS, WE, 18, 22, 300, 600,
                 [(ts(0), 20.0), (ts(1), 23.0), (ts(3), 24.0), (ts(7, 40), 20.0)])
    assert r["verdict"] == "fail"
    assert r["max_continuous_exceeded_seconds"] == 400.0


def test_exceeded_exactly_at_limit_does_not_fail():
    # 严格“超过”才不合格：300 == 300 不算
    r = evaluate(WS, WE, 18, 22, 300, 600,
                 [(ts(0), 20.0), (ts(1), 23.0), (ts(6), 20.0)])
    assert r["max_continuous_exceeded_seconds"] == 300.0
    assert r["verdict"] == "pending"  # 有尾部未知 -> 待定


def test_interrupted_exceedance_takes_longest_run():
    r = evaluate(WS, WE, 18, 22, 600, 600,
                 [(ts(0), 23.0), (ts(2), 20.0), (ts(5), 23.0), (ts(10), 20.0)])
    assert r["max_continuous_exceeded_seconds"] == 300.0


def test_full_coverage_pass():
    # 首读在窗口起点、末读在窗口终点，间隔均 <=120s，温度全正常
    readings = [(WS + timedelta(seconds=60 * m), 20.0) for m in range(61)]
    r = evaluate(WS, WE, 18, 22, 300, 120, readings)
    assert r["verdict"] == "pass"
    assert r["unknown_segments"] == []
    assert len(r["normal_segments"]) == 1
    assert r["normal_segments"][0]["duration_seconds"] == 3600.0


def test_temperature_bounds_are_inclusive():
    r = evaluate(WS, WE, 18, 22, 300, 120,
                 [(ts(0), 18.0), (ts(1), 22.0), (ts(2), 17.999), (ts(3), 20.0)])
    assert len(r["exceeded_segments"]) == 1
    assert r["exceeded_segments"][0]["duration_seconds"] == 60.0
    assert r["normal_segments"][0]["duration_seconds"] == 120.0


def test_timezone_offsets_normalized_to_utc():
    start = datetime(2026, 10, 1, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    end = datetime(2026, 10, 1, 9, 0, tzinfo=timezone(timedelta(hours=8)))
    r = evaluate(start, end, 18, 22, 300, 4000,
                 [(start, 20.0), (end, 20.0)])
    assert r["verdict"] == "pass"
    assert r["normal_segments"][0]["start"] == "2026-10-01T00:00:00Z"


def test_segments_partition_entire_window_without_gaps():
    r = evaluate(WS, WE, 18, 22, 300, 200,
                 [(ts(1), 23.0), (ts(2), 20.0), (ts(6), 21.0)])
    allsegs = (r["normal_segments"] + r["exceeded_segments"] + r["unknown_segments"])
    total = sum(s["duration_seconds"] for s in allsegs)
    assert total == 3600.0
    # 首尾相接
    allsegs.sort(key=lambda s: s["start"])
    for a, b in zip(allsegs, allsegs[1:]):
        assert a["end"] == b["start"]


def test_unsorted_input_sorted_by_algorithm():
    r1 = evaluate(WS, WE, 18, 22, 300, 600,
                  [(ts(0), 20.0), (ts(1), 23.0), (ts(2), 20.0)])
    r2 = evaluate(WS, WE, 18, 22, 300, 600,
                  [(ts(2), 20.0), (ts(0), 20.0), (ts(1), 23.0)])
    assert r1["exceeded_segments"] == r2["exceeded_segments"]


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
