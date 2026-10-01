#!/usr/bin/env python3
"""博物馆展柜温度暴露审定服务 —— Python 调用示例（仅用标准库）。

用法：
    python examples/python_client.py                 # 默认 http://localhost:8000
    BASE_URL=http://127.0.0.1:8000 python examples/python_client.py

演示内容：
  1) 创建三个审定窗口，分别得到 待定 / 不合格 / 合格；
  2) 幂等提交、同修订号内容冲突 409、旧修订号 409、高修订号替换重算；
  3) 窗口外读数、同刻读数拒收；
  4) 首次签发、修订后再次签发形成新版本，旧版本快照保持不变；
  5) 采样仪器批量补传：整批事务、逐项 created/updated/idempotent、
     互换采样时刻、整批冲突回滚、重放不产生新修订；
  6) 误采作废与恢复：作废留存修订（原因/时间/前一版本）、幂等重放、
     拒绝路径、更高修订号恢复（单条与批量）、证书与报告反映移除/再出现；
  7) 窗口规则版本化修订：初始规则第 1 版、连续修订号（当前版+1）生效、
     同号同值同原因幂等/冲突 409、旧号与跳号 409、非法输入 422、
     立即重算、既有证书快照不变，跨规则版本的修订影响报告分别依两张
     证书快照重建时间线并给出四项规则前后值。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")


def call(method: str, path: str, body: dict | None = None, expect=(200, 201)):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode())
            assert resp.status in expect, f"{method} {path} -> {resp.status}"
            return resp.status, payload
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        try:
            detail = json.loads(detail).get("detail", detail)
        except json.JSONDecodeError:
            pass
        return e.code, {"detail": detail}


def show(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def summary(ev: dict) -> str:
    return (f"结论={ev['verdict']}  最长连续超限={ev['max_continuous_exceeded_seconds']}s "
            f"(限值 {ev['max_exceed_seconds']}s)  "
            f"正常段={len(ev['normal_segments'])} 超限段={len(ev['exceeded_segments'])} "
            f"未知段={len(ev['unknown_segments'])}")


def create_window(name, minutes, max_exceed, max_gap):
    _, w = call("POST", "/api/v1/windows", {
        "name": name,
        "start_at": "2026-10-01T00:00:00Z",
        "end_at": f"2026-10-01T00:{minutes:02d}:00Z",
        "allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
        "max_continuous_exceed_seconds": max_exceed,
        "max_sample_interval_seconds": max_gap,
    })
    return w["id"]


def submit(wid, rid, rev, minute, temp, minute_end=60):
    ts = f"2026-10-01T00:{minute:02d}:00Z"
    return call("POST", f"/api/v1/windows/{wid}/readings",
                {"reading_id": rid, "revision": rev,
                 "measured_at": ts, "temperature": temp})


def submit_batch(wid, items):
    return call("POST", f"/api/v1/windows/{wid}/readings/batch",
                {"readings": items})


def void(wid, rid, rev, reason):
    return call("POST", f"/api/v1/windows/{wid}/readings/{rid}/void",
                {"revision": rev, "reason": reason})


def revise_rules(wid, rev, *, tmin=18.0, tmax=22.0, max_exceed=300.0, max_gap=600.0,
                 reason="季节切换"):
    return call("POST", f"/api/v1/windows/{wid}/rule-revisions", {
        "revision": rev,
        "allowed_temp_min": tmin, "allowed_temp_max": tmax,
        "max_continuous_exceed_seconds": max_exceed,
        "max_sample_interval_seconds": max_gap,
        "reason": reason,
    })


def item(rid, rev, minute, temp):
    return {"reading_id": rid, "revision": rev,
            "measured_at": f"2026-10-01T00:{minute:02d}:00Z",
            "temperature": temp}


def main() -> None:
    print("服务：", BASE_URL)
    print("健康检查：", call("GET", "/health")[1])

    # ---------- 窗口 1：待定（采样间隔超上限 -> 未知） ----------
    show("窗口1：间隔超限产生未知区段 -> 待定")
    w1 = create_window("展柜A-间隔超限", 10, max_exceed=300, max_gap=120)
    for rid, m, temp in [("a", 0, 20.0), ("b", 2, 20.5), ("c", 9, 20.0), ("d", 10, 20.0)]:
        code, r = submit(w1, rid, 1, m, temp)
        assert code == 201, (code, r)
    ev = call("GET", f"/api/v1/windows/{w1}/evaluation")[1]
    print(summary(ev))
    assert ev["verdict"] == "pending"

    # ---------- 窗口 2：不合格（连续超限 7 分钟 > 5 分钟） ----------
    show("窗口2：连续超限 420s 超过限值 300s -> 不合格")
    w2 = create_window("展柜B-超温", 10, max_exceed=300, max_gap=600)
    for rid, m, temp in [("a", 0, 20.0), ("b", 1, 23.0), ("c", 7, 24.0),
                         ("d", 8, 20.0), ("e", 10, 20.0)]:
        assert submit(w2, rid, 1, m, temp)[0] == 201
    ev = call("GET", f"/api/v1/windows/{w2}/evaluation")[1]
    print(summary(ev))
    for s in ev["exceeded_segments"]:
        print("  超限段:", s["start"], "->", s["end"], f"{s['duration_seconds']}s")
    assert ev["verdict"] == "fail"
    assert ev["max_continuous_exceeded_seconds"] == 420.0

    # ---------- 窗口 3：合格（满覆盖、温度全在区间） ----------
    show("窗口3：首尾边界有读数、间隔合法、温度正常 -> 合格")
    w3 = create_window("展柜C-正常", 5, max_exceed=300, max_gap=120)
    for m in range(6):
        assert submit(w3, f"r{m}", 1, m, 20.0)[0] == 201
    ev = call("GET", f"/api/v1/windows/{w3}/evaluation")[1]
    print(summary(ev))
    assert ev["verdict"] == "pass"

    # ---------- 幂等 / 冲突 / 修订 ----------
    show("幂等、冲突与修订规则（窗口3 上演示）")
    code, r = submit(w3, "r1", 1, 1, 20.0)
    print("同 ID+修订号重复提交(内容一致):", code, r.get("effect"))
    assert code == 201 and r["effect"] == "idempotent"

    code, r = submit(w3, "r1", 1, 1, 21.5)
    print("同 ID+修订号但温度冲突:", code, "-", r["detail"][:60], "...")
    assert code == 409

    code, r = call("POST", f"/api/v1/windows/{w3}/readings",
                   {"reading_id": "r1", "revision": 1,
                    "measured_at": "2026-10-01T00:01:30Z", "temperature": 20.0})
    print("同 ID+修订号但时间戳冲突:", code)
    assert code == 409

    code, r = submit(w3, "r2", 1, 2, 23.0)  # 既有 r2 rev1=20.0，同号改温度 -> 409
    print("对既有读数用同修订号改温度:", code)
    assert code == 409
    code, r = submit(w3, "r2", 3, 2, 20.0)  # 更高修订号，内容相同也算替换
    print("高修订号替换:", code, r["effect"], "->", summary(r["current_evaluation"]))
    assert code == 201 and r["effect"] == "updated"
    code, r = submit(w3, "r2", 1, 2, 20.0)  # 再提旧修订号 -> 409
    print("替换后旧修订号拒绝:", code)
    assert code == 409

    # ---------- 拒收 ----------
    show("无效读数拒收")
    code, r = call("POST", f"/api/v1/windows/{w3}/readings",
                   {"reading_id": "x", "revision": 1,
                    "measured_at": "2026-10-01T02:00:00Z", "temperature": 20.0})
    print("窗口外读数:", code, "-", r["detail"] if isinstance(r, dict) else r)
    assert code == 422
    code, r = call("POST", f"/api/v1/windows/{w3}/readings",
                   {"reading_id": "naive", "revision": 1,
                    "measured_at": "2026-10-01T00:03:00", "temperature": 20.0})
    print("不带时区的时间戳:", code)
    assert code == 422
    code, r = call("POST", f"/api/v1/windows/{w3}/readings",
                   {"reading_id": "dup", "revision": 1,
                    "measured_at": "2026-10-01T00:01:00Z", "temperature": 19.0})
    print("同刻不同读数:", code, "-", r.get("detail", "")[:60])
    assert code == 422

    # ---------- 签发版本与不可变快照 ----------
    show("签发：版本化 + 快照不可变")
    # 先让窗口2变成“带超温已确认”状态并签发 v1
    c1 = call("POST", f"/api/v1/windows/{w2}/certificates/issue", {"note": "首签-不合格"})[1]
    print("v1:", c1["version"], c1["verdict"], c1["verdict_label_zh"])
    assert c1["version"] == 1 and c1["verdict"] == "fail"

    # 修订超温读数后再签发 v2
    code, _ = submit(w2, "b", 2, 1, 20.0)
    code, _ = submit(w2, "c", 2, 7, 20.0)
    assert code == 201
    c2 = call("POST", f"/api/v1/windows/{w2}/certificates/issue", {"note": "整改后重签"})[1]
    print("v2:", c2["version"], c2["verdict"], c2["verdict_label_zh"])
    assert c2["version"] == 2

    old = call("GET", f"/api/v1/certificates/{c1['id']}")[1]
    print("旧版 v1 仍为:", old["verdict"], "，快照中读数修订号:",
          [x["revision"] for x in old["snapshot"]["readings"]])
    assert old["verdict"] == "fail"

    versions = call("GET", f"/api/v1/windows/{w2}/certificates")[1]
    print("版本列表:", [(v["version"], v["verdict"]) for v in versions])

    # ---------- 修订影响报告（仅依据两张证书的不可变快照） ----------
    show("修订影响报告：比对窗口2 的 v1（不合格）与 v2（整改后）快照")
    path = (f"/api/v1/certificates/revision-report"
            f"?old_certificate_id={c1['id']}&new_certificate_id={c2['id']}")
    code, rep = call("GET", path)
    assert code == 200, (code, rep)
    print("读数差异：")
    for ch in rep["readings_diff"]["changed"]:
        print(f"  - {ch['reading_id']}: 变更字段 {ch['changed_fields']}  "
              f"rev {ch['before']['revision']}->{ch['after']['revision']}, "
              f"温度 {ch['before']['temperature']}->{ch['after']['temperature']}")
    print("状态变化的半开区间（时间线）：")
    for seg in rep["timeline"]["changes"]:
        ev0, ev1 = seg["before"]["evidence"], seg["after"]["evidence"]
        print(f"  - [{seg['start'][11:19]}, {seg['end'][11:19]}) "
              f"{seg['before']['status']}({ev0}) -> {seg['after']['status']}({ev1}) "
              f"{seg['duration_seconds']:g}s")
    print("汇总结论：", rep["summary"]["conclusion_zh"])

    # 拒绝路径：版本倒序 / 跨窗口 / 同 ID -> 422；证书不存在 -> 404
    assert call("GET", f"/api/v1/certificates/revision-report"
                       f"?old_certificate_id={c2['id']}&new_certificate_id={c1['id']}")[0] == 422
    assert call("GET", f"/api/v1/certificates/revision-report"
                       f"?old_certificate_id={c1['id']}&new_certificate_id={c1['id']}")[0] == 422
    assert call("GET", f"/api/v1/certificates/revision-report"
                       f"?old_certificate_id={c1['id']}&new_certificate_id=99999")[0] == 404
    print("倒序/同 ID 已拒绝（422），不存在证书已拒绝（404）")

    # ---------- 采样仪器批量补传（整批事务） ----------
    show("批量补传：整批提交、最终状态同刻唯一、互换时刻、重放幂等")
    wb = create_window("展柜D-批量补传", 10, max_exceed=300, max_gap=600)

    code, r = submit_batch(wb, [item("a", 1, 0, 20.0), item("b", 1, 2, 23.0),
                                item("c", 1, 9, 20.0)])
    print("批量创建:", code, [x["effect"] for x in r["results"]],
          "->", summary(r["current_evaluation"]))
    assert code == 201 and [x["effect"] for x in r["results"]] == \
        ["created", "created", "created"]

    # 同批重放：全部幂等，不产生新修订
    code, r = submit_batch(wb, [item("a", 1, 0, 20.0), item("b", 1, 2, 23.0),
                                item("c", 1, 9, 20.0)])
    print("同批重放:", [x["effect"] for x in r["results"]])
    assert [x["effect"] for x in r["results"]] == ["idempotent"] * 3

    # 两条读数以更高修订号互换采样时刻：按最终状态不冲突，顺序无关
    code, r = submit_batch(wb, [item("a", 2, 2, 20.0), item("b", 2, 0, 20.0)])
    print("互换时刻:", [(x["reading"]["reading_id"], x["effect"],
                        x["reading"]["measured_at"][11:19]) for x in r["results"]])
    assert [x["effect"] for x in r["results"]] == ["updated", "updated"]
    rows = call("GET", f"/api/v1/windows/{wb}/readings")[1]
    ts = {x["reading_id"]: x["measured_at"] for x in rows}
    assert ts["a"].endswith("T00:02:00Z") and ts["b"].endswith("T00:00:00Z")

    # 任一项冲突/无效 -> 整批拒收，活动读数与修订历史均不变
    code, r = submit_batch(wb, [item("b", 1, 0, 20.0),   # 旧修订号 -> 409
                                item("x", 1, 5, 20.0)])   # 本应 created，但一起回滚
    print("含旧修订号的批次:", code, "（整批拒收，x 不落库）")
    assert code == 409
    code, r = submit_batch(wb, [item("a", 3, 3, 20.0), item("a", 3, 4, 20.0)])
    print("同批重复 reading_id:", code)
    assert code == 422
    code, r = submit_batch(wb, [])
    print("空批次:", code)
    assert code == 422
    ids_now = {x["reading_id"] for x in call("GET", f"/api/v1/windows/{wb}/readings")[1]}
    assert ids_now == {"a", "b", "c"}

    # ---------- 误采作废与恢复 ----------
    show("误采作废：留存作废修订、立即重算、幂等与拒绝路径")
    wv = create_window("展柜E-误采作废", 10, max_exceed=300, max_gap=600)
    for rid, m, temp in [("a", 0, 20.0), ("b", 2, 23.0), ("c", 9, 20.0), ("d", 10, 20.0)]:
        assert submit(wv, rid, 1, m, temp)[0] == 201
    ev = call("GET", f"/api/v1/windows/{wv}/evaluation")[1]
    assert ev["verdict"] == "fail"  # b 在 00:02->00:09 超限 420s > 300

    # 作废 b：更高修订号 rev2 + 非空原因；当前审定立即剔除该读数并重算
    code, r = void(wv, "b", 2, "误采：传感器未校准")
    print("作废 b:", code, r["effect"], "->", summary(r["current_evaluation"]))
    assert code == 201 and r["effect"] == "voided"
    assert r["current_evaluation"]["verdict"] == "pass"  # 超限源剔除后满覆盖正常
    rec = r["void"]
    print("作废记录: rev", rec["revision"], "原因:", rec["reason"],
          "操作时间:", rec["voided_at"], "前一版本:", rec["previous"])
    assert rec["previous"] == {"revision": 1, "measured_at": "2026-10-01T00:02:00Z",
                               "temperature": 23.0}

    # 相同作废修订号+相同原因重放 -> 幂等；原因不同/低修订号 -> 409；从未存在 -> 404
    code, r = void(wv, "b", 2, "误采：传感器未校准")
    print("作废重放:", code, r["effect"])
    assert code == 201 and r["effect"] == "idempotent"
    assert void(wv, "b", 2, "另一个原因")[0] == 409
    assert void(wv, "b", 1, "误采")[0] == 409
    assert void(wv, "ghost", 1, "误采")[0] == 404
    # 已作废读数不能再次以新修订号作废；空原因 422
    assert void(wv, "b", 3, "再次作废")[0] == 409
    assert void(wv, "b", 3, "   ")[0] == 422
    print("拒绝路径：原因不同/低修订号/再次作废 409，从未存在 404，空原因 422")

    # 修订历史：作废作为该 ID 的修订留存（原因/操作时间/前一版本）
    revs = call("GET", f"/api/v1/windows/{wv}/readings/b/revisions")[1]
    print("b 的修订历史:", [(x["revision"], x["status"]) for x in revs])
    assert [(x["revision"], x["status"]) for x in revs] == [(1, "superseded"), (2, "voided")]
    assert revs[1]["void_reason"] == "误采：传感器未校准" and revs[1]["prev_revision"] == 1

    # 签发 v1（作废后）；旧版提交不得复活（rev1 重放 -> 409）
    cv1 = call("POST", f"/api/v1/windows/{wv}/certificates/issue", {"note": "作废后"})[1]
    assert [x["reading_id"] for x in cv1["snapshot"]["readings"]] == ["a", "c", "d"]
    code, r = submit(wv, "b", 1, 2, 23.0)
    print("旧版提交复活尝试:", code)
    assert code == 409

    # 恢复：提交更高修订号 rev3 -> restored，立即重算
    code, r = submit(wv, "b", 3, 2, 20.5)
    print("恢复 b:", code, r["effect"], "->", summary(r["current_evaluation"]))
    assert code == 201 and r["effect"] == "restored"
    cv2 = call("POST", f"/api/v1/windows/{wv}/certificates/issue", {"note": "恢复后"})[1]

    # 修订影响报告：v1->v2 呈现 b 再出现（added）；v1 快照保持原样
    rep = call("GET", f"/api/v1/certificates/revision-report"
                      f"?old_certificate_id={cv1['id']}&new_certificate_id={cv2['id']}")[1]
    print("报告 v1->v2: added =", [x["reading_id"] for x in rep["readings_diff"]["added"]],
          "removed =", [x["reading_id"] for x in rep["readings_diff"]["removed"]])
    assert [x["reading_id"] for x in rep["readings_diff"]["added"]] == ["b"]
    old = call("GET", f"/api/v1/certificates/{cv1['id']}")[1]
    assert [x["reading_id"] for x in old["snapshot"]["readings"]] == ["a", "c", "d"]

    # 批量恢复：作废 c/d 后整批以更高修订号恢复（均 restored）
    assert void(wv, "c", 2, "误采")[0] == 201
    assert void(wv, "d", 2, "误采")[0] == 201
    code, r = submit_batch(wv, [item("c", 3, 9, 20.0), item("d", 3, 10, 20.0)])
    print("批量恢复:", code, [x["effect"] for x in r["results"]])
    assert code == 201 and [x["effect"] for x in r["results"]] == ["restored", "restored"]
    # 批量中任一项冲突 -> 整批回滚（b 恢复尝试随批回滚，仍保持作废）
    assert void(wv, "c", 4, "再次误采")[0] == 201
    code, r = submit_batch(wv, [item("c", 5, 9, 20.0),      # 本应 restored
                                item("a", 1, 0, 99.0)])     # 同 ID 同修订号内容冲突 -> 409
    print("含冲突项的恢复批次:", code, "（整批回滚，c 仍未恢复）")
    assert code == 409
    ids_now = {x["reading_id"] for x in call("GET", f"/api/v1/windows/{wv}/readings")[1]}
    assert ids_now == {"a", "b", "d"}

    # ---------- 窗口规则版本化修订 ----------
    show("规则修订：初始规则第 1 版，连续修订号生效；立即重算；快照不变")
    wr = create_window("展柜F-规则修订", 10, max_exceed=300, max_gap=600)
    for rid, m in [("a", 0), ("b", 5), ("z", 10)]:
        assert submit(wr, rid, 1, m, 20.0)[0] == 201
    assert call("GET", f"/api/v1/windows/{wr}/evaluation")[1]["verdict"] == "pass"
    cr1 = call("POST", f"/api/v1/windows/{wr}/certificates/issue",
               {"note": "规则第1版"})[1]
    print("签发时快照规则修订号:", cr1["snapshot"]["window"]["rule_revision"])

    # 目标号 = 当前版 + 1（即 2）：温度上限收到 19.5，20.0 全部超限
    code, r = revise_rules(wr, 2, tmax=19.5, reason="换季：收窄允许温度带")
    print("修订为第 2 版:", code, r["effect"],
          f"{r['rule_revision']['before']['allowed_temp_max']} -> "
          f"{r['rule_revision']['after']['allowed_temp_max']}",
          "->", summary(r["current_evaluation"]))
    assert code == 201 and r["effect"] == "revised"
    assert r["current_evaluation"]["verdict"] == "fail"

    # 同号 + 同四项值 + 同原因重放 -> 幂等，不留新记录
    code, r = revise_rules(wr, 2, tmax=19.5, reason="换季：收窄允许温度带")
    print("同号同值同原因重放:", code, r["effect"])
    assert code == 201 and r["effect"] == "idempotent"
    # 同号但原因不同 / 旧号 / 跳号 -> 409；非法值 / 空原因 -> 422
    assert revise_rules(wr, 2, tmax=19.5, reason="别的原因")[0] == 409
    assert revise_rules(wr, 1, reason="旧号")[0] == 409
    assert revise_rules(wr, 9, tmax=19.5, reason="跳号")[0] == 409
    assert revise_rules(wr, 3, tmin=23.0, tmax=22.0, reason="区间倒置")[0] == 422
    assert revise_rules(wr, 3, max_gap=0, reason="非正时长")[0] == 422
    assert revise_rules(wr, 3, tmax=19.5, reason="   ")[0] == 422
    print("冲突/旧号/跳号 409，区间倒置/非正时长/空原因 422（均不留新记录）")
    revs = call("GET", f"/api/v1/windows/{wr}/rule-revisions")[1]
    print("规则修订历史:", [(x["rule_revision"], x["reason"][:12]) for x in revs])
    assert [x["rule_revision"] for x in revs] == [1, 2]

    # 窗口起止与读数不变；既有证书快照保持第 1 版规则；新签发采用第 2 版
    detail = call("GET", f"/api/v1/windows/{wr}")[1]
    assert detail["rule_revision"] == 2 and len(detail["active_readings"]) == 3
    old = call("GET", f"/api/v1/certificates/{cr1['id']}")[1]
    assert old["snapshot"]["window"]["allowed_temp_max"] == 22.0
    cr2 = call("POST", f"/api/v1/windows/{wr}/certificates/issue",
               {"note": "规则第2版"})[1]
    assert cr2["snapshot"]["window"]["rule_revision"] == 2 and cr2["verdict"] == "fail"

    # 跨规则版本的修订影响报告：分别依两证快照重建时间线 + 四项规则前后值
    rep = call("GET", f"/api/v1/certificates/revision-report"
                      f"?old_certificate_id={cr1['id']}&new_certificate_id={cr2['id']}")[1]
    rd = rep["rules_diff"]
    print("报告规则差异: 第", rd["rule_revision_before"], "版 -> 第",
          rd["rule_revision_after"], "版, 变化字段 =", rd["changed_fields"])
    assert rd["changed_fields"] == ["allowed_temp_max"]
    assert all(c["before"]["status"] == "normal" for c in rep["timeline"]["changes"])
    assert all(c["after"]["status"] == "exceeded" for c in rep["timeline"]["changes"])
    print("汇总结论：", rep["summary"]["conclusion_zh"])

    print("\n全部示例执行完成 ✔")


if __name__ == "__main__":
    main()
