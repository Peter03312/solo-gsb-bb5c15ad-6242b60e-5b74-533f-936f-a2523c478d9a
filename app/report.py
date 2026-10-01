"""证书修订影响报告（纯函数，无 IO，可独立测试）。

仅依据两张**已签发证书的不可变快照**比对，不读取当前活动读数、不重查窗口：

1. 读数级：按 reading_id 对齐两版快照，比较 revision / measured_at / temperature，
   列出新增、变更与移除（误采作废后再次签发的证书快照中该读数消失，计入
   removed；恢复后再次签发则计入 added）。
2. 时间线级：用两版各自快照里的窗口参数与读数，重建“带证据”的原始半开
   区段（采样保持、采样间隔超限未知、边界空白未知），再按两版全部区段
   边界的并集切分，输出**状态发生变化**的原子半开区间，给出变化前后状态
   及各侧对应读数证据（reading_id + revision）。未知侧必须说明来源：
     - sample_gap：相邻读数间隔严格超过最大采样间隔（附间隔两侧读数）；
     - boundary_gap：边界空白，boundary_side=leading/trailing/empty
       （首读之前附右侧首条读数，末读之后附左侧末条读数，整窗无读数时
       两侧均无读数）。
   相邻且变化前后状态与证据完全相同的区间合并输出。
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Sequence

from .models import VERDICT_LABELS_ZH
from .timeutils import parse_stored, to_iso

# 未知原因
UNKNOWN_SAMPLE_GAP = "sample_gap"      # 采样间隔超限
UNKNOWN_BOUNDARY_GAP = "boundary_gap"  # 边界空白（首读前 / 末读后 / 整窗无读数）

# 边界空白方位
BOUNDARY_LEADING = "leading"
BOUNDARY_TRAILING = "trailing"
BOUNDARY_EMPTY = "empty"

# 结论严重度：用于报告中判定修订使结论改善还是恶化
_VERDICT_SEVERITY = {"pass": 0, "pending": 1, "fail": 2}

# 变更字段固定输出顺序
_FIELD_ORDER = ("revision", "measured_at", "temperature")

# 规则四项固定输出顺序（与 services.RULE_FIELDS 同义，报告内自持常量）
RULE_FIELD_ORDER = (
    "allowed_temp_min",
    "allowed_temp_max",
    "max_continuous_exceed_seconds",
    "max_sample_interval_seconds",
)
RULE_LABELS_ZH = {
    "allowed_temp_min": "允许温度下限",
    "allowed_temp_max": "允许温度上限",
    "max_continuous_exceed_seconds": "最长连续超限时长",
    "max_sample_interval_seconds": "最大采样间隔",
}
RULE_UNITS_ZH = {
    "allowed_temp_min": "°C",
    "allowed_temp_max": "°C",
    "max_continuous_exceed_seconds": "s",
    "max_sample_interval_seconds": "s",
}


@dataclass(frozen=True)
class EvidenceSeg:
    """带读数证据的原始半开区段 [start, end)。"""

    start: datetime
    end: datetime
    status: str  # normal | exceeded | unknown

    # 未知来源：sample_gap / boundary_gap；已知区段为 None
    unknown_reason: Optional[str] = None
    # boundary_gap 时的方位：leading / trailing / empty
    boundary_side: Optional[str] = None

    # 已知区段（采样保持）：覆盖该区段的读数（区间左侧那条）
    reading_id: Optional[str] = None
    revision: Optional[int] = None

    # sample_gap：间隔两侧读数
    left_reading_id: Optional[str] = None
    left_revision: Optional[int] = None
    right_reading_id: Optional[str] = None
    right_revision: Optional[int] = None

    def evidence_key(self) -> tuple[Any, ...]:
        """证据身份：状态 + 未知来源/方位 + 各侧读数 ID/修订号。"""
        return (
            self.status,
            self.unknown_reason,
            self.boundary_side,
            self.reading_id,
            self.revision,
            self.left_reading_id,
            self.left_revision,
            self.right_reading_id,
            self.right_revision,
        )

    def evidence(self) -> dict[str, Any]:
        """输出单侧证据 dict（已知区段只给读数 ID/修订号）。"""
        if self.status != "unknown":
            return {"reading_id": self.reading_id, "revision": self.revision}

        out: dict[str, Any] = {"unknown_reason": self.unknown_reason}
        if self.unknown_reason == UNKNOWN_BOUNDARY_GAP:
            out["boundary_side"] = self.boundary_side
            if self.boundary_side == BOUNDARY_LEADING:
                # 首读之前：右侧首条读数是最近的有效读数
                out["right_reading_id"] = self.right_reading_id
                out["right_revision"] = self.right_revision
            elif self.boundary_side == BOUNDARY_TRAILING:
                # 末读之后：左侧末条读数是最近的有效读数
                out["left_reading_id"] = self.left_reading_id
                out["left_revision"] = self.left_revision
            # empty：整窗无任何读数，两侧均无读数可引用
            return out

        # sample_gap：间隔两侧读数（正是“间隔超限”的证据）
        out["left_reading_id"] = self.left_reading_id
        out["left_revision"] = self.left_revision
        out["right_reading_id"] = self.right_reading_id
        out["right_revision"] = self.right_revision
        return out


# ------------------------------------------------------------ 快照解析

def _snapshot_window(snap: dict[str, Any]) -> dict[str, Any]:
    return snap["window"]


def _snapshot_readings(snap: dict[str, Any]) -> list[dict[str, Any]]:
    """快照读数按时间升序返回（快照签发时已排序，这里不依赖写入顺序）。"""
    out = list(snap.get("readings", []))
    out.sort(key=lambda r: parse_stored(r["measured_at"]))
    return out


def build_evidence_segments(snapshot: dict[str, Any]) -> list[EvidenceSeg]:
    """按快照参数与读数重建带证据的原始半开区段。

    与 app.evaluation 的覆盖语义保持一致（采样保持、间隔严格超限未知、
    边界空白未知），区别仅在于这里保留每段的读数身份证据，且不合并
    （合并发生在两版对齐之后）。
    """
    win = _snapshot_window(snapshot)
    win_start = parse_stored(win["start_at"])
    win_end = parse_stored(win["end_at"])
    max_gap = float(win["max_sample_interval_seconds"])
    temp_min = float(win["allowed_temp_min"])
    temp_max = float(win["allowed_temp_max"])

    readings = [
        (parse_stored(r["measured_at"]), float(r["temperature"]),
         r["reading_id"], int(r["revision"]))
        for r in _snapshot_readings(snapshot)
    ]

    if not readings:
        # 整窗无有效读数：未知来自边界空白（既无首读也无末读）
        return [EvidenceSeg(win_start, win_end, "unknown",
                            unknown_reason=UNKNOWN_BOUNDARY_GAP,
                            boundary_side=BOUNDARY_EMPTY)]

    segs: list[EvidenceSeg] = []
    first_ts, _, first_rid, first_rev = readings[0]
    if first_ts > win_start:
        segs.append(EvidenceSeg(
            win_start, first_ts, "unknown",
            unknown_reason=UNKNOWN_BOUNDARY_GAP, boundary_side=BOUNDARY_LEADING,
            right_reading_id=first_rid, right_revision=first_rev,
        ))

    for i, (ts, temp, rid, rev) in enumerate(readings):
        if i + 1 >= len(readings):
            break
        next_ts, _, next_rid, next_rev = readings[i + 1]
        gap = (next_ts - ts).total_seconds()
        if gap > max_gap:
            segs.append(EvidenceSeg(
                ts, next_ts, "unknown",
                unknown_reason=UNKNOWN_SAMPLE_GAP,
                left_reading_id=rid, left_revision=rev,
                right_reading_id=next_rid, right_revision=next_rev,
            ))
        else:
            status = "exceeded" if (temp < temp_min or temp > temp_max) else "normal"
            segs.append(EvidenceSeg(ts, next_ts, status,
                                    reading_id=rid, revision=rev))

    last_ts, _, last_rid, last_rev = readings[-1]
    if last_ts < win_end:
        segs.append(EvidenceSeg(
            last_ts, win_end, "unknown",
            unknown_reason=UNKNOWN_BOUNDARY_GAP, boundary_side=BOUNDARY_TRAILING,
            left_reading_id=last_rid, left_revision=last_rev,
        ))

    return segs


# ------------------------------------------------------------ 时间线对齐

def _seg_at(segs: Sequence[EvidenceSeg], t: datetime) -> EvidenceSeg:
    starts = [s.start for s in segs]
    i = bisect_right(starts, t) - 1
    seg = segs[i]
    assert seg.start <= t < seg.end, f"时刻 {t} 未被区段覆盖"
    return seg


def _timeline_change(seg: EvidenceSeg, start: datetime, end: datetime,
                     old: EvidenceSeg, new: EvidenceSeg) -> dict[str, Any]:
    return {
        "start": to_iso(start),
        "end": to_iso(end),
        "duration_seconds": round((end - start).total_seconds(), 3),
        "before": {"status": old.status, "evidence": old.evidence()},
        "after": {"status": new.status, "evidence": new.evidence()},
    }


def merge_adjacent_changes(changes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并相邻且变化前后状态与证据均相同的区间。

    输入为按时间升序、互不重叠的变化原子区间；仅当两段首尾相接，且
    before/after 的（状态, 证据）分别完全一致时才合并。被“无变化”区间
    隔开的两段不合并。
    """
    merged: list[dict[str, Any]] = []
    for ch in changes:
        if merged and merged[-1]["end"] == ch["start"]:
            prev, cur = merged[-1], ch
            same = (
                prev["before"]["status"] == cur["before"]["status"]
                and prev["after"]["status"] == cur["after"]["status"]
                and prev["before"]["evidence"] == cur["before"]["evidence"]
                and prev["after"]["evidence"] == cur["after"]["evidence"]
            )
            if same:
                start_dt = parse_stored(prev["start"])
                end_dt = parse_stored(ch["end"])
                merged[-1] = {
                    "start": prev["start"],
                    "end": ch["end"],
                    "duration_seconds": round((end_dt - start_dt).total_seconds(), 3),
                    "before": prev["before"],
                    "after": prev["after"],
                }
                continue
        merged.append(dict(ch))
    return merged


def align_timelines(old_snap: dict[str, Any],
                    new_snap: dict[str, Any]) -> list[dict[str, Any]]:
    """按两版全部区段边界对齐，输出状态发生变化的半开区间（已合并）。"""
    old_segs = build_evidence_segments(old_snap)
    new_segs = build_evidence_segments(new_snap)

    win = _snapshot_window(old_snap)
    win_start = parse_stored(win["start_at"])
    win_end = parse_stored(win["end_at"])

    boundaries = {win_start, win_end}
    for segs in (old_segs, new_segs):
        for s in segs:
            boundaries.add(s.start)
            boundaries.add(s.end)
    cuts = sorted(boundaries)

    changes: list[dict[str, Any]] = []
    for b0, b1 in zip(cuts, cuts[1:]):
        if b0 == b1:
            continue
        old_seg = _seg_at(old_segs, b0)
        new_seg = _seg_at(new_segs, b0)
        if old_seg.status != new_seg.status:
            changes.append(_timeline_change(old_seg, b0, b1, old_seg, new_seg))

    return merge_adjacent_changes(changes)


# ------------------------------------------------------------ 读数差异

def _reading_key(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "reading_id": r["reading_id"],
        "revision": int(r["revision"]),
        "measured_at": to_iso(parse_stored(r["measured_at"])),
        "temperature": float(r["temperature"]),
    }


def diff_readings(old_snap: dict[str, Any],
                  new_snap: dict[str, Any]) -> dict[str, Any]:
    """按 reading_id 比较两版快照读数：新增 / 移除 / 变更。

    误采作废后再次签发的快照中该读数消失 -> removed；恢复后再次出现 -> added。
    """
    old_map = {r["reading_id"]: _reading_key(r) for r in _snapshot_readings(old_snap)}
    new_map = {r["reading_id"]: _reading_key(r) for r in _snapshot_readings(new_snap)}

    added, removed, changed = [], [], []

    for rid in sorted(new_map.keys() - old_map.keys()):
        added.append(new_map[rid])
    for rid in sorted(old_map.keys() - new_map.keys()):
        removed.append(old_map[rid])
    for rid in sorted(old_map.keys() & new_map.keys()):
        before, after = old_map[rid], new_map[rid]
        changed_fields = [f for f in _FIELD_ORDER if before[f] != after[f]]
        if changed_fields:
            changed.append({
                "reading_id": rid,
                "before": before,
                "after": after,
                "changed_fields": changed_fields,
            })

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "added_count": len(added),
        "removed_count": len(removed),
        "changed_count": len(changed),
    }


# ------------------------------------------------------------ 规则差异

def _snapshot_rule_revision(snap: dict[str, Any]) -> int:
    """快照所属规则修订号；规则版本化之前签发的旧快照按第 1 版处理。"""
    return int(snap.get("window", {}).get("rule_revision", 1))


def diff_rules(old_snap: dict[str, Any], new_snap: dict[str, Any]) -> dict[str, Any]:
    """比较两版快照的窗口规则：各自规则修订号 + 四项规则前后值与变化。

    时间线（align_timelines）已分别依据两张快照的规则参数重建，因此即使
    审定结论未变，这里仍明确给出四项规则的前后值与 changed_fields，使
    跨规则版本的修订影响可以直接看出规则变更。
    """
    ow, nw = _snapshot_window(old_snap), _snapshot_window(new_snap)
    old_rev, new_rev = _snapshot_rule_revision(old_snap), _snapshot_rule_revision(new_snap)

    changes = []
    changed_fields = []
    for f in RULE_FIELD_ORDER:
        old_v, new_v = float(ow[f]), float(nw[f])
        if old_v != new_v:
            changed_fields.append(f)
            changes.append({
                "field": f,
                "label_zh": RULE_LABELS_ZH[f],
                "before": old_v,
                "after": new_v,
                "delta": round(new_v - old_v, 3),
                "unit_zh": RULE_UNITS_ZH[f],
            })

    return {
        "rule_revision_before": old_rev,
        "rule_revision_after": new_rev,
        "rules_changed": bool(changed_fields),
        "changed_fields": changed_fields,
        "before": {"rule_revision": old_rev, **{f: float(ow[f]) for f in RULE_FIELD_ORDER}},
        "after": {"rule_revision": new_rev, **{f: float(nw[f]) for f in RULE_FIELD_ORDER}},
        "changes": changes,
    }


# ------------------------------------------------------------ 汇总结论

def _verdict_direction(old_v: str, new_v: str) -> str:
    d_old = _VERDICT_SEVERITY.get(old_v, -1)
    d_new = _VERDICT_SEVERITY.get(new_v, -1)
    if d_new > d_old:
        return "恶化"
    if d_new < d_old:
        return "改善"
    return "持平"


def _build_summary(old_cert: dict[str, Any], new_cert: dict[str, Any],
                   reading_diff: dict[str, Any],
                   changes: Sequence[dict[str, Any]],
                   rule_diff: dict[str, Any]) -> dict[str, Any]:
    old_ev = old_cert["snapshot"]["evaluation"]
    new_ev = new_cert["snapshot"]["evaluation"]
    old_v, new_v = old_cert["verdict"], new_cert["verdict"]
    old_max = float(old_ev["max_continuous_exceeded_seconds"])
    new_max = float(new_ev["max_continuous_exceeded_seconds"])
    limit = float(new_ev["max_exceed_seconds"])
    delta = round(new_max - old_max, 3)
    if delta > 0:
        delta_text = f"增加 {delta:g}s"
    elif delta < 0:
        delta_text = f"减少 {abs(delta):g}s"
    else:
        delta_text = "持平"
    direction = _verdict_direction(old_v, new_v)

    lines = [
        f"审定结论：{VERDICT_LABELS_ZH.get(old_v, old_v)}（{old_v}）→ "
        f"{VERDICT_LABELS_ZH.get(new_v, new_v)}（{new_v}），{direction}。",
        f"最长连续超限时长：{old_max:g}s → {new_max:g}s（{delta_text}；限值 {limit:g}s）。",
    ]
    if rule_diff["rules_changed"]:
        parts = []
        for ch in rule_diff["changes"]:
            d = ch["delta"]
            d_text = f"{'增加' if d > 0 else '减少'} {abs(d):g}{ch['unit_zh']}"
            parts.append(
                f"{ch['label_zh']} {ch['before']:g} → {ch['after']:g}{ch['unit_zh']}"
                f"（{d_text}）"
            )
        lines.append(
            f"规则由第 {rule_diff['rule_revision_before']} 版修订为第 "
            f"{rule_diff['rule_revision_after']} 版：{'；'.join(parts)}。"
        )
    else:
        lines.append(
            f"规则版本：第 {rule_diff['rule_revision_before']} 版 → 第 "
            f"{rule_diff['rule_revision_after']} 版，四项规则值无变化。"
        )
    lines.append(
        f"读数差异：新增 {reading_diff['added_count']} 条、"
        f"变更 {reading_diff['changed_count']} 条、"
        f"移除 {reading_diff['removed_count']} 条（均以两张证书的不可变快照为据）。"
    )
    if changes:
        lines.append(f"时间线共有 {len(changes)} 个半开区间状态发生变化（相邻同证据区间已合并）。")
    else:
        lines.append("两版时间线在共同审定窗口内状态分区完全一致。")

    return {
        "verdict_before": old_v,
        "verdict_after": new_v,
        "verdict_before_label_zh": VERDICT_LABELS_ZH.get(old_v, old_v),
        "verdict_after_label_zh": VERDICT_LABELS_ZH.get(new_v, new_v),
        "verdict_changed": old_v != new_v,
        "verdict_direction": direction,
        "max_continuous_exceeded_seconds_before": old_max,
        "max_continuous_exceeded_seconds_after": new_max,
        "max_continuous_exceeded_seconds_delta": delta,
        "max_exceed_seconds_limit": limit,
        "timeline_change_count": len(changes),
        "conclusion_zh": " ".join(lines),
    }


# ------------------------------------------------------------ 报告入口

def build_revision_report(old_cert: dict[str, Any],
                          new_cert: dict[str, Any]) -> dict[str, Any]:
    """组装两张证书（含 snapshot 的 certificate dict）的修订影响报告。

    调用方负责保证两证存在、属于同一审定窗口且版本严格按先后排列。
    """
    old_snap, new_snap = old_cert["snapshot"], new_cert["snapshot"]
    rule_diff = diff_rules(old_snap, new_snap)
    reading_diff = diff_readings(old_snap, new_snap)
    changes = align_timelines(old_snap, new_snap)
    summary = _build_summary(old_cert, new_cert, reading_diff, changes, rule_diff)

    win = dict(new_snap["window"])

    def cert_ref(c: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": c["id"],
            "window_id": c["window_id"],
            "version": c["version"],
            "verdict": c["verdict"],
            "verdict_label_zh": c["verdict_label_zh"],
            "issued_at": c["issued_at"],
            "note": c.get("note"),
        }

    return {
        "window": win,
        "old_certificate": cert_ref(old_cert),
        "new_certificate": cert_ref(new_cert),
        "rules_diff": rule_diff,
        "readings_diff": reading_diff,
        "timeline": {
            "max_sample_interval_seconds": float(win["max_sample_interval_seconds"]),
            "changes": changes,
            "change_count": len(changes),
        },
        "summary": summary,
    }
