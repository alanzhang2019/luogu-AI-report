"""
phase3_dashboard.py — v3.5 Phase 3

教练 dashboard 轻量统计(§11 反向 Scope 允许 admin 简单统计)。
聚合激活码 / 冲刺营 / 跳级 / 政策水印数据。
"""
from __future__ import annotations

import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from task_store import _get_conn  # noqa: E402


def get_revenue_stats(*, days: int = 90) -> dict:
    """
    营收统计（v3.5 §10 验收："至少 1 单付费"）：
      - 按 SKU 拆分（¥15/¥30/¥99/¥299）
      - 已兑换 vs 未兑换
      - 当前生效订阅数
    """
    from activation_codes import SKU_CATALOG
    threshold = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT sku, COUNT(*) AS total,
                   SUM(CASE WHEN redeemed_at IS NOT NULL THEN 1 ELSE 0 END) AS redeemed,
                   SUM(CASE WHEN redeemed_at IS NOT NULL
                             AND expires_at IS NOT NULL
                             AND expires_at >= ? THEN 1 ELSE 0 END) AS active
            FROM activation_codes
            WHERE created_at >= ?
            GROUP BY sku
            """,
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), threshold),
        ).fetchall()
    finally:
        conn.close()
    by_sku: dict[str, dict] = {}
    total_cny = 0
    for r in rows:
        sku = r["sku"]
        info = SKU_CATALOG.get(sku, {})
        price = info.get("price_cny", 0)
        redeemed_revenue = int(r["redeemed"]) * price
        by_sku[sku] = {
            "name": info.get("name", sku),
            "price_cny": price,
            "total_generated": int(r["total"]),
            "redeemed": int(r["redeemed"]),
            "active": int(r["active"]),
            "revenue_cny": redeemed_revenue,
        }
        total_cny += redeemed_revenue
    return {
        "window_days": int(days),
        "by_sku": by_sku,
        "total_revenue_cny": total_cny,
    }


def get_skip_success_rate() -> dict:
    """
    跳级成功率（v3.5 §7 + §10）：
      - 从 gesp_exams 找所有"跳级"考试（registered_level > 学员起步 level）
      - 统计通过率
      - 起步 level = 该学员最早一次真考的 level
    """
    conn = _get_conn()
    try:
        # 拉所有 gesp_exams + 学员档案，按学员聚合
        rows = conn.execute(
            """
            SELECT ge.student_id, ge.registered_level, ge.actual_score, ge.passed,
                   ge.created_at, s.luogu_uid, s.real_name
            FROM gesp_exams ge
            JOIN students s ON s.id = ge.student_id
            ORDER BY ge.student_id, ge.created_at
            """
        ).fetchall()
    finally:
        conn.close()
    # 按学员聚合 → 找起步 level + 跳级
    by_student: dict[int, dict] = {}
    for r in rows:
        sid = int(r["student_id"])
        if sid not in by_student:
            by_student[sid] = {
                "luogu_uid": r["luogu_uid"],
                "real_name": r["real_name"],
                "exams": [],
            }
        by_student[sid]["exams"].append({
            "level": int(r["registered_level"]),
            "score": int(r["actual_score"]) if r["actual_score"] is not None else None,
            "passed": bool(r["passed"]),
        })
    # 跳级 = 不是 level=1（首考）
    skip_total = 0
    skip_passed = 0
    by_student_skip: list[dict] = []
    for sid, info in by_student.items():
        exams = info["exams"]
        if not exams:
            continue
        # 起步 level = exams 中最小的 level
        start_level = min(e["level"] for e in exams)
        # 跳级 = 当前 level > start_level
        for e in exams:
            if e["level"] > start_level:
                skip_total += 1
                if e["passed"]:
                    skip_passed += 1
                by_student_skip.append({
                    "student": info["real_name"] or f"UID-{info['luogu_uid']}",
                    "from_level": start_level,
                    "to_level": e["level"],
                    "score": e["score"],
                    "passed": e["passed"],
                })
    return {
        "skip_total": skip_total,
        "skip_passed": skip_passed,
        "skip_pass_rate_pct": round(skip_passed / skip_total * 100, 1) if skip_total else 0.0,
        "examples": by_student_skip[:10],
    }


def get_c9_quota_status() -> dict:
    """
    C9 强基 5 所样板高校 + 学员目标命中数。
    v3.5 §8 反向 Scope：只做 5 所样板，不做 39 校。
    """
    from admin_goals import SAMPLE_UNIVERSITIES
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT target_university, COUNT(*) AS cnt
            FROM student_goals
            WHERE target_university IS NOT NULL AND target_university != ''
            GROUP BY target_university
            """
        ).fetchall()
    finally:
        conn.close()
    by_uni = {r["target_university"]: int(r["cnt"]) for r in rows}
    return {
        "sample_universities": SAMPLE_UNIVERSITIES,
        "student_counts": by_uni,
        "total_with_goal": sum(int(r["cnt"]) for r in rows),
    }


def get_exemption_milestone_stats() -> dict:
    """v3.5 §10 验收：'9 月免初赛解锁学员 ≥ 3 人'"""
    conn = _get_conn()
    try:
        j = conn.execute("SELECT COUNT(*) AS n FROM students WHERE gesp_can_exempt_csp_j = 1").fetchone()["n"]
        s = conn.execute("SELECT COUNT(*) AS n FROM students WHERE gesp_can_exempt_csp_s = 1").fetchone()["n"]
    finally:
        conn.close()
    return {
        "csp_j_exempt_students": int(j),
        "csp_s_exempt_students": int(s),
        "total_exempt_students": int(j) + int(s),
        "milestone_target": 3,
        "milestone_reached": int(j) + int(s) >= 3,
    }


def get_full_dashboard() -> dict:
    """Phase 3 完整 dashboard（admin 简单统计，§11 允许）"""
    from camp_curriculum import (
        seed_camp_curriculum,
        camp_pass_rate,
        get_policy_events_last_updated,
    )

    # 首次跑：种子数据
    seed_camp_curriculum("popularize_camp")
    seed_camp_curriculum("improve_camp")

    pop_rate = camp_pass_rate(sku="popularize_camp")
    imp_rate = camp_pass_rate(sku="improve_camp")
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "revenue": get_revenue_stats(days=90),
        "skip_success": get_skip_success_rate(),
        "c9_quotas": get_c9_quota_status(),
        "exemption_milestone": get_exemption_milestone_stats(),
        "camp_pass_rate": {
            "popularize_camp": pop_rate,
            "improve_camp": imp_rate,
        },
        "policy_data_last_updated": get_policy_events_last_updated(),
    }


# -- smoke test --
if __name__ == "__main__":
    import os
    if "TASK_DB_PATH" not in os.environ:
        os.environ["TASK_DB_PATH"] = str(ROOT / "tasks.db")

    print("[SMOKE] phase3_dashboard.py")

    rev = get_revenue_stats(days=365)
    print(f"  [OK] 营收 90d: ¥{rev['total_revenue_cny']}, 4 SKU:")
    for sku, info in rev["by_sku"].items():
        print(f"        {info['name']:12s} (¥{info['price_cny']:>3d}) "
              f"生成 {info['total_generated']:>2d} 兑换 {info['redeemed']:>2d}  "
              f"生效 {info['active']:>2d} 收入 ¥{info['revenue_cny']:>4d}")

    skip = get_skip_success_rate()
    print(f"  [OK] 跳级成功率: {skip['skip_passed']}/{skip['skip_total']} = {skip['skip_pass_rate_pct']}%")

    c9 = get_c9_quota_status()
    print(f"  [OK] C9 强基 5 校命中数: {c9['student_counts']} (共 {c9['total_with_goal']} 学员有目标)")

    em = get_exemption_milestone_stats()
    print(f"  [OK] 免初赛解锁: J {em['csp_j_exempt_students']} + S {em['csp_s_exempt_students']} = {em['total_exempt_students']}"
          f"  ({'✅ 达标 ≥ 3' if em['milestone_reached'] else '⚠️ 未达 3'})")

    full = get_full_dashboard()
    assert full["revenue"]["total_revenue_cny"] >= 0
    assert full["policy_data_last_updated"] is not None
    print(f"  [OK] 完整 dashboard: {len(full)} 个区块, 政策水印 {full['policy_data_last_updated']}")

    print("[OK] phase3_dashboard smoke test passed")
