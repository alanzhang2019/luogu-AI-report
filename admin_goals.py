"""
admin_goals.py — v3.5 Phase 2

学员目标路径（primary_path）+ 目标大学 / 省份。
基于 task_store.py 的 student_goals 表。

v3.5 §4.2 定义 6 种 primary_path：
  - '保送'             NOI 集训队 / 省队 → 强基/保送
  - '强基'             强基计划 → 清北 C9
  - '综评'             综评招生
  - '文化课保底'        OI + 文化课并行
  - '兴趣探索'          单纯培养兴趣
  - '未决定'           默认值
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from task_store import _get_conn  # noqa: E402

ALLOWED_PRIMARY_PATHS = {
    "保送",
    "强基",
    "综评",
    "文化课保底",
    "兴趣探索",
    "未决定",
}

# 5 所样板高校（v3.5 §2.3 强基 5 所样板：清北复交浙）
SAMPLE_UNIVERSITIES = [
    "清华大学",
    "北京大学",
    "复旦大学",
    "上海交通大学",
    "浙江大学",
]


def upsert_student_goal(
    student_id: int,
    *,
    primary_path: str = "未决定",
    target_university: str | None = None,
    target_province: str | None = None,
    notes: str | None = None,
) -> int:
    """
    创建或更新学员目标路径。
    student_goals 表 student_id 是 PRIMARY KEY，天然 UPSERT。
    """
    if int(student_id) <= 0:
        raise ValueError("student_id 无效")
    primary_path = (primary_path or "未决定").strip()
    if primary_path not in ALLOWED_PRIMARY_PATHS:
        raise ValueError(f"primary_path 必须是 {sorted(ALLOWED_PRIMARY_PATHS)} 之一")

    conn = _get_conn()
    try:
        conn.execute(
            """
            INSERT INTO student_goals (
                student_id, primary_path, target_university, target_province, notes, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(student_id) DO UPDATE SET
                primary_path = excluded.primary_path,
                target_university = excluded.target_university,
                target_province = excluded.target_province,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (
                int(student_id),
                primary_path,
                (target_university or None),
                (target_province or None),
                (notes or None),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return int(student_id)


def get_student_goal(student_id: int) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM student_goals WHERE student_id = ?", (int(student_id),)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_students_with_goals() -> list[dict]:
    """LEFT JOIN students + student_goals，方便批量查看"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT s.id AS student_id, s.luogu_uid, s.real_name, s.school, s.grade,
                   g.primary_path, g.target_university, g.target_province, g.notes,
                   g.updated_at
            FROM students s
            LEFT JOIN student_goals g ON g.student_id = s.id
            ORDER BY s.id DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def recommend_skip_path(student_id: int) -> dict:
    """
    根据学员 primary_path + GESP 状态给出"跳级 vs 稳扎稳打"建议。

    规则（v3.5 §1.5 + §2.2 场景 A/B/C）：
      - primary_path = '保送' / '强基' → 鼓励跳级（节省 1-2 次考试）
      - primary_path = '文化课保底' / '兴趣探索' → 建议稳扎稳打 N+1
      - primary_path = '综评' / '未决定' → 默认 N+1，跳级需 90+ 才有意义
    """
    import admin_students  # 延迟导入避免循环
    progress = admin_students.get_student_gesp_progress(int(student_id))
    goal = get_student_goal(int(student_id)) or {}
    primary_path = goal.get("primary_path") or "未决定"

    if not progress:
        return {
            "primary_path": primary_path,
            "next_eligible_level": 1,
            "recommendation": "学员档案不存在",
            "options": [],
        }

    next_lv = int(progress.get("next_eligible_level") or 1)
    latest_score = progress["student"].get("gesp_latest_score")

    # 找最近一次真考的等级
    exams = progress.get("exams") or []
    last_exam = exams[0] if exams else None
    current_level = int(last_exam["registered_level"]) if last_exam else 0
    last_score = int(last_exam["actual_score"]) if last_exam else None

    if primary_path in ("保送", "强基"):
        # 高目标 → 鼓励跳级
        recommend = "B. 跳 1 级" if (last_score or 0) >= 90 else "A. 报 N+1（稳）"
        reasoning = (
            f"目标 {primary_path}，建议优先抢时间窗口（强基/保送关键期）。"
            + ("最近一次 90+ 跳级条件已满足，可跳 1 级。" if (last_score or 0) >= 90
               else "近期未达 90 分，建议稳扎稳打保通过率。")
        )
    elif primary_path in ("文化课保底", "兴趣探索"):
        recommend = "A. 报 N+1（稳）"
        reasoning = f"目标 {primary_path}，不抢时间窗口，稳扎稳打 N+1 通过率更高。"
    else:
        # 综评 / 未决定
        if (last_score or 0) >= 90:
            recommend = "B. 跳 1 级"
            reasoning = "最近一次 90+ 跳级机制已触发，可考虑跳 1 级节省 1 次考试。"
        else:
            recommend = "A. 报 N+1（稳）"
            reasoning = "未达 90 分，建议稳扎稳打报 N+1。"

    return {
        "primary_path": primary_path,
        "current_level": current_level,
        "last_score": last_score,
        "next_eligible_level": next_lv,
        "recommendation": recommend,
        "reasoning": reasoning,
        "can_exempt_csp_j": bool(progress.get("can_exempt_csp_j")),
        "can_exempt_csp_s": bool(progress.get("can_exempt_csp_s")),
    }


# -- smoke test --
if __name__ == "__main__":
    import os
    if "TASK_DB_PATH" not in os.environ:
        os.environ["TASK_DB_PATH"] = str(ROOT / "tasks.db")

    print("[SMOKE] admin_goals.py")

    import admin_students  # noqa: E402
    test_uid = "test_goal_smoke_001"
    existing = admin_students.get_student_by_uid(test_uid)
    if existing:
        admin_students.delete_student(existing["id"])
    sid = admin_students.create_student(test_uid, real_name="目标测试")

    # 1) upsert
    upsert_student_goal(sid, primary_path="强基", target_university="清华大学", target_province="北京")
    g1 = get_student_goal(sid)
    assert g1 and g1["primary_path"] == "强基"
    assert g1["target_university"] == "清华大学"
    print(f"  [OK] upsert: {g1['primary_path']} / {g1['target_university']}")

    # 2) update（未传的字段 → NULL，符合"表单回填"语义）
    upsert_student_goal(sid, primary_path="保送", notes="改主意了")
    g2 = get_student_goal(sid)
    assert g2["primary_path"] == "保送"
    assert g2["notes"] == "改主意了"
    assert g2["target_university"] is None  # 未传 → NULL
    print(f"  [OK] update primary_path → {g2['primary_path']}, 未传字段置 NULL")

    # 2b) update 保留原值（显式传入）
    upsert_student_goal(
        sid, primary_path="保送",
        target_university="北京大学", target_province="北京",
    )
    g2b = get_student_goal(sid)
    assert g2b["target_university"] == "北京大学"
    print(f"  [OK] 显式传值 → target_university={g2b['target_university']}")

    # 3) 非法 primary_path
    try:
        upsert_student_goal(sid, primary_path="非法路径")
    except ValueError as e:
        print(f"  [OK] 非法路径被拒: {e}")
    else:
        raise AssertionError("应拒绝非法路径")

    # 4) recommend_skip_path
    rec = recommend_skip_path(sid)
    assert rec["primary_path"] == "保送"
    print(f"  [OK] recommend: {rec['recommendation']}")
    print(f"       reasoning: {rec['reasoning']}")

    # 5) list_students_with_goals
    rows = list_students_with_goals()
    assert any(r["student_id"] == sid for r in rows)
    print(f"  [OK] list_students_with_goals = {len(rows)}")

    # 6) 清理
    admin_students.delete_student(sid)
    assert get_student_goal(sid) is None
    print(f"  [OK] 清理完毕")

    print("[OK] admin_goals smoke test passed")
