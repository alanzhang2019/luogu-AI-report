"""
seed_demo_students.py — 写 3 个 demo 学员 + 各自 GESP 历史

idempotent：每个 demo 学员用固定 luogu_uid，重跑不会重复创建。

学员画像（覆盖 v3.5 §1.5 三种典型场景）：
  1. 999101 · 张同学  · 5 年级 · 已过 GESP 1-4 全部 (高分 4)  → 跳级备选
  2. 999102 · 李同学  · 初二 · 已过 GESP 1-5                → 6 级即将报考
  3. 999103 · 王同学  · 高一 · 已过 GESP 1-7 (高分)         → csp_j 免初赛
"""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "docs"))

from gesp_estimator import compute_exemptions  # noqa: E402

import admin_students  # noqa: E402
from task_store import _get_conn  # noqa: E402


DEMOS = [
    {
        "luogu_uid": "999101",
        "real_name": "张同学",
        "school": "海淀实验小学",
        "grade": "2024",
        "is_minor": True,  # 5 年级 < 14
        "note": "算法天赋强，5 年级 1 年打通 GESP 1-4",
        "gesp_history": [
            # 4 个不同场次（3/6/9/12 月 L1-4）
            (1, 88, "%L1-4%", 2026, 3),
            (2, 92, "%L1-4%", 2026, 6),
            (3, 75, "%L1-4%", 2026, 9),
            (4, 95, "%L1-4%", 2026, 12),  # 4 级 95 → 跳级
        ],
    },
    {
        "luogu_uid": "999102",
        "real_name": "李同学",
        "school": "人大附中",
        "grade": "2024",
        "is_minor": False,
        "note": "稳扎稳打型，GESP 5 级刚好过线",
        "gesp_history": [
            (1, 70, "%L1-4%", 2026, 3),
            (2, 75, "%L1-4%", 2026, 6),
            (3, 80, "%L1-4%", 2026, 9),
            (4, 65, "%L1-4%", 2026, 12),
            (5, 62, "%L5-6%", 2026, 3),  # 5 级 62 → 升 6
        ],
    },
    {
        "luogu_uid": "999103",
        "real_name": "王同学",
        "school": "北京四中",
        "grade": "2025",
        "is_minor": False,
        "note": "高一冲省队，GESP 7 80+ 已解锁 CSP-J 免初赛",
        "gesp_history": [
            (1, 95, "%L1-4%", 2026, 3),
            (2, 90, "%L1-4%", 2026, 6),
            (3, 92, "%L1-4%", 2026, 9),
            (4, 88, "%L1-4%", 2026, 12),
            (5, 82, "%L5-6%", 2026, 3),
            (6, 78, "%L5-8%", 2026, 12),  # 6 级用 12 月 L5-8 冬考
            (7, 85, "%L7-8%", 2026, 9),   # 7 级 85 → csp_j 免
        ],
    },
]


def _resolve_exam_id(cursor, code_pattern: str, year: int, month: int) -> int:
    """从 competitions 表找一个匹配 code + year + month 的 exam_id。
    用 code LIKE + exam_date 月份范围精确匹配，避免不同场次用同一 exam_id。
    """
    start = f"{year:04d}-{month:02d}-01"
    # 该月最后一天
    if month == 12:
        end = f"{year + 1:04d}-01-01"
    else:
        end = f"{year:04d}-{month + 1:02d}-01"
    row = cursor.execute(
        "SELECT id FROM competitions WHERE type='gesp' AND code LIKE ? "
        "AND exam_date >= ? AND exam_date < ? ORDER BY exam_date ASC LIMIT 1",
        (code_pattern, start, end),
    ).fetchone()
    if not row:
        raise RuntimeError(
            f"未找到 GESP 赛事（pattern={code_pattern} year={year} month={month}），请先跑 import_competitions.py"
        )
    return int(row["id"])


def main() -> int:
    print(f"[INFO] 写入 {len(DEMOS)} 个 demo 学员")

    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    try:
        for d in DEMOS:
            existing = admin_students.get_student_by_uid(d["luogu_uid"])
            if existing:
                conn.execute("DELETE FROM gesp_exams WHERE student_id = ?", (existing["id"],))
                conn.commit()
                student_id = existing["id"]
                print(f"  [skip] 学员 #{student_id} {d['luogu_uid']} 已存在，复用并重写 GESP 记录")
            else:
                student_id = admin_students.create_student(
                    luogu_uid=d["luogu_uid"],
                    real_name=d["real_name"],
                    school=d["school"],
                    grade=d["grade"],
                    is_minor=d["is_minor"],
                    note=d["note"],
                )
                print(f"  [new ] 学员 #{student_id} {d['luogu_uid']} {d['real_name']}")

            # 录 GESP 记录（用 (year, month) 精确匹配不同场次）
            for level, score, code_pattern, year, month in d["gesp_history"]:
                exam_id = _resolve_exam_id(conn, code_pattern, year, month)
                eid = admin_students.add_gesp_exam(
                    student_id=student_id,
                    exam_id=exam_id,
                    registered_level=level,
                    actual_score=score,
                    certificate_no=f"GESP-DEMO-{d['luogu_uid']}-{level}",
                    notes=None,
                    recorded_by="seed",
                )
                exempts = compute_exemptions(level, score)
                tag = " ".join(f"免{e}" for e in exempts) if exempts else ""
                print(f"         GESP {level} 级 {score} 分 ({year}-{month:02d} {code_pattern}) → {tag or '记录'}")
    finally:
        conn.close()

    print()
    print(f"[OK] 写入完成。当前学员总数：{admin_students.count_students()}")
    print()
    print("预览方式：")
    print("  1. CLI 速览：python preview_phase1.py")
    print("  2. Flask 启动：python web_app.py")
    print("     登录 admin 后访问 /admin/students")
    return 0


if __name__ == "__main__":
    sys.exit(main())
