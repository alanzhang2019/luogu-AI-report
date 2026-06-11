"""
admin_students.py — v3.5 Phase 1

学员档案 CRUD + GESP 状态计算。
所有函数都基于 task_store.py 的 SQLite 表（students / gesp_exams / competitions）。

业务规则（v3.5 §1.2-1.3）：
  - students.is_minor 必填
  - GESP 7 级 80+ 触发 gesp_can_exempt_csp_j
  - GESP 8 级 60+ 触发 gesp_can_exempt_csp_j
  - GESP 8 级 80+ 触发 gesp_can_exempt_csp_s
  - 每次 add_gesp_exam() 自动重算 students 6 个 GESP 字段
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from gesp_estimator import (  # noqa: E402
    compute_exemptions,
    gesp_progress_bar,
    next_eligible_gesp_level,
)

from task_store import _get_conn  # noqa: E402


# ========== 学员 CRUD ==========

def create_student(
    luogu_uid: str,
    *,
    real_name: str | None = None,
    school: str | None = None,
    grade: str | None = None,
    is_minor: bool = False,
    note: str | None = None,
    city: str | None = None,
    province: str | None = None,  # v3.8 · 省份（用于家长版报告本地政策匹配）
    gender: str | None = None,
    birth_date: str | None = None,
    registered_via: str = "admin",
) -> int:
    """创建学员，返回新 id。luogu_uid 必填且唯一。

    v3.5.2 新增（学而思图 1 模式）：
      - city / gender / birth_date / registered_via 4 字段
      - 14 岁以下 + 无授权 → real_name 强制 NULL（PIPL §5.2 防护）

    v3.8 新增：
      - province 字段（用于本地升学政策匹配）
    """
    if not str(luogu_uid or "").strip():
        raise ValueError("luogu_uid 必填")
    # 14 岁以下 + 无授权 → real_name 强制 NULL（PIPL §5.2 防护）
    if is_minor and real_name:
        real_name = None
    # gender 限制在 M/F/空
    if gender not in (None, "", "M", "F"):
        raise ValueError("gender 必须是 M / F / 空")
    conn = _get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO students
              (luogu_uid, real_name, school, grade, is_minor, note,
               city, gender, birth_date, registered_via)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(luogu_uid).strip(),
                (real_name or None),
                (school or None),
                (grade or None),
                1 if is_minor else 0,
                (note or None),
                (city or None),
                (gender or None),
                (birth_date or None),
                registered_via or "admin",
            ),
        )
        # v3.8 · 单独 UPDATE province（兼容老 schema 中可能不存在的列）
        if province:
            try:
                conn.execute(
                    "UPDATE students SET province = ? WHERE id = ?",
                    ((province or "").strip(), int(cur.lastrowid)),
                )
            except Exception:
                pass
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def get_student(student_id: int) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM students WHERE id = ?", (int(student_id),)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_student_by_uid(luogu_uid: str) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM students WHERE luogu_uid = ?", (str(luogu_uid).strip(),)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_students(*, limit: int = 50, offset: int = 0) -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT s.*,
                   (SELECT COUNT(*) FROM gesp_exams g WHERE g.student_id = s.id) AS gesp_exam_count
            FROM students s
            ORDER BY s.created_at DESC, s.id DESC
            LIMIT ? OFFSET ?
            """,
            (int(limit), int(offset)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_students() -> int:
    conn = _get_conn()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM students").fetchone()[0])
    finally:
        conn.close()


def delete_student(student_id: int) -> bool:
    """删除学员，级联删 gesp_exams / student_competitions / weekly_reports"""
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM gesp_exams WHERE student_id = ?", (int(student_id),))
        conn.execute(
            "DELETE FROM student_competitions WHERE student_id = ?", (int(student_id),)
        )
        conn.execute(
            "DELETE FROM weekly_reports WHERE student_id = ?", (int(student_id),)
        )
        conn.execute("DELETE FROM student_cookies WHERE student_id = ?", (int(student_id),))
        conn.execute("DELETE FROM student_goals WHERE student_id = ?", (int(student_id),))
        conn.execute("DELETE FROM guardians WHERE student_id = ?", (int(student_id),))
        cur = conn.execute("DELETE FROM students WHERE id = ?", (int(student_id),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ========== GESP 真考记录 ==========

def add_gesp_exam(
    student_id: int,
    exam_id: int | None,  # v3.7 · None 时按 award_year+registered_level 自动查/建 competition
    registered_level: int,
    actual_score: int,
    *,
    certificate_no: str | None = None,
    notes: str | None = None,
    recorded_by: str = "admin",
    award_year: int | None = None,
) -> int:
    """录入 GESP 真考分数，自动计算 passed / can_skip_next / exempts_csp_j / exempts_csp_s
    并更新 students 表的 6 个 GESP 字段。

    v3.7 升级：自录入学员（/generate-form 提交时）可传 exam_id=None，
    自动按 award_year + registered_level 查找 competitions 表；
    若对应年份/级别不存在则自动创建一条 GESP competition 占位记录。
    返回 gesp_exams.id。"""
    if not (1 <= int(registered_level) <= 8):
        raise ValueError("registered_level 必须在 1-8")
    if not (0 <= int(actual_score) <= 100):
        raise ValueError("actual_score 必须在 0-100")

    passed = bool(actual_score >= 60)
    can_skip_next = bool(actual_score >= 90)
    exempts = compute_exemptions(int(registered_level), int(actual_score))
    exempts_csp_j = "csp_j" in exempts
    exempts_csp_s = "csp_s" in exempts

    conn = _get_conn()
    try:
        # v3.7 · 自录入场景：exam_id 缺省时自动按 year+level 匹配或创建 competition
        if exam_id is None or int(exam_id) <= 0:
            target_year = int(award_year) if award_year else int(date.today().year)
            level = int(registered_level)
            row = conn.execute(
                "SELECT id FROM competitions WHERE type='gesp' AND level=? AND data_year=? LIMIT 1",
                (level, target_year),
            ).fetchone()
            if row:
                exam_id = int(row["id"])
            else:
                cur = conn.execute(
                    """
                    INSERT INTO competitions
                        (code, name, type, level, exam_date, data_year, notes)
                    VALUES (?, ?, 'gesp', ?, ?, ?, 'auto-created for self-registered student')
                    """,
                    (
                        f"gesp_l{level}_{target_year}",
                        f"GESP {level} 级 {target_year} 年考试（自录）",
                        level,
                        f"{target_year}-12-31",
                        target_year,
                    ),
                )
                exam_id = int(cur.lastrowid)

        # UPSERT：UNIQUE(student_id, exam_id) 冲突时替换
        cur = conn.execute(
            """
            INSERT INTO gesp_exams (
                student_id, exam_id, registered_level, actual_score,
                passed, can_skip_next, exempts_csp_j, exempts_csp_s,
                certificate_no, notes, recorded_by, award_year
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(student_id, exam_id) DO UPDATE SET
                registered_level = excluded.registered_level,
                actual_score = excluded.actual_score,
                passed = excluded.passed,
                can_skip_next = excluded.can_skip_next,
                exempts_csp_j = excluded.exempts_csp_j,
                exempts_csp_s = excluded.exempts_csp_s,
                certificate_no = excluded.certificate_no,
                notes = excluded.notes,
                recorded_by = excluded.recorded_by,
                award_year = excluded.award_year
            """,
            (
                int(student_id),
                int(exam_id),
                int(registered_level),
                int(actual_score),
                int(passed),
                int(can_skip_next),
                int(exempts_csp_j),
                int(exempts_csp_s),
                certificate_no or None,
                notes or None,
                recorded_by,
                int(award_year) if award_year else None,
            ),
        )
        exam_pk = int(cur.lastrowid)

        # 重新聚合 students 表的 6 个 GESP 字段
        _recompute_student_gesp_state(conn, int(student_id))
        conn.commit()
        return exam_pk
    finally:
        conn.close()


def _recompute_student_gesp_state(conn: sqlite3.Connection, student_id: int) -> None:
    """从 gesp_exams 重算 students 6 字段：gesp_highest_passed / latest_score /
    can_exempt_csp_j / can_exempt_csp_s / exemption_expiry / next_eligible_level"""
    rows = conn.execute(
        """
        SELECT registered_level, actual_score, exempts_csp_j, exempts_csp_s
        FROM gesp_exams
        WHERE student_id = ? AND actual_score IS NOT NULL
        ORDER BY registered_level DESC, actual_score DESC
        """,
        (student_id,),
    ).fetchall()

    if not rows:
        return

    highest_passed = 0
    latest_score = None
    has_csp_j = False
    has_csp_s = False
    last_exam = None  # 用于算 next_eligible_level

    # highest_passed = 通过的最高等级（actual_score >= 60）
    for r in rows:
        if r["actual_score"] >= 60 and r["registered_level"] > highest_passed:
            highest_passed = r["registered_level"]
    # latest_score = 最近一次的实际分（按 gesp_exams.id 最大）
    latest_row = conn.execute(
        "SELECT actual_score FROM gesp_exams WHERE student_id = ? AND actual_score IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (student_id,),
    ).fetchone()
    latest_score = int(latest_row["actual_score"]) if latest_row else None
    # can_exempt_csp_j / csp_s = 任一记录触发即可
    for r in rows:
        if r["exempts_csp_j"]:
            has_csp_j = True
        if r["exempts_csp_s"]:
            has_csp_s = True
    # next_eligible_level = 最近一次考试 → 跳级算法
    last_row = conn.execute(
        "SELECT registered_level, actual_score FROM gesp_exams "
        "WHERE student_id = ? AND actual_score IS NOT NULL ORDER BY id DESC LIMIT 1",
        (student_id,),
    ).fetchone()
    if last_row:
        last_exam = {
            "registered_level": int(last_row["registered_level"]),
            "actual_score": int(last_row["actual_score"]),
        }
    next_lv = next_eligible_gesp_level(last_exam)

    # exemption_expiry 占位：1 年有效期（v3.5 文档说"待 CCF 公告"）
    expiry = date.today().replace(year=date.today().year + 1).isoformat()

    conn.execute(
        """
        UPDATE students SET
            gesp_highest_passed = ?,
            gesp_latest_score = ?,
            gesp_can_exempt_csp_j = ?,
            gesp_can_exempt_csp_s = ?,
            gesp_exemption_expiry = ?,
            gesp_next_eligible_level = ?
        WHERE id = ?
        """,
        (
            int(highest_passed),
            int(latest_score) if latest_score is not None else None,
            1 if has_csp_j else 0,
            1 if has_csp_s else 0,
            expiry,
            int(next_lv),
            int(student_id),
        ),
    )


# ========== v3.5.3 CSP/NOIP/NOI 历史奖项自录入 ==========

# 录入选项（供 UI 渲染与校验）
CSP_AWARD_TYPES = [
    ("csp_j_pre",   "CSP-J 初赛（入门级）"),
    ("csp_j_final", "CSP-J 复赛（入门级）"),
    ("csp_s_pre",   "CSP-S 初赛（提高级）"),
    ("csp_s_final", "CSP-S 复赛（提高级）"),
    ("noip_1",      "NOIP 一等（省赛）"),
    ("noi_bronze",  "NOI 铜牌"),
    ("noi_silver",  "NOI 银牌"),
    ("noi_gold",    "NOI 金牌"),
]

CSP_AWARD_LEVELS = [
    ("excellent", "优秀"),
    ("first",     "一等"),
    ("second",    "二等"),
    ("third",     "三等"),
    ("bronze",    "铜牌"),
    ("silver",    "银牌"),
    ("gold",      "金牌"),
]

CSP_AWARD_TYPE_SET = {t[0] for t in CSP_AWARD_TYPES}
CSP_AWARD_LEVEL_SET = {l[0] for l in CSP_AWARD_LEVELS}


def add_csp_award(
    student_id: int,
    competition_type: str,
    award_level: str,
    award_year: int,
    *,
    actual_score: int | None = None,
    province: str | None = None,
    certificate_no: str | None = None,
    notes: str | None = None,
    recorded_by: str = "self",
) -> int:
    """录入 CSP/NOIP/NOI 历史奖项（学员自录入或教练代录）。

    Args:
        competition_type: 比赛类型（CSP_AWARD_TYPES 中的 code）
        award_level: 奖项等级（CSP_AWARD_LEVELS 中的 code）
        award_year: 获奖年份（2015-2030）
        actual_score: 实际分（可选，复赛才有）
        province: 省份（省赛才有，全国赛可空）
        certificate_no: 证书编号（可选）
        recorded_by: 录入人（'self' = 学员自录）

    Returns:
        csp_awards.id

    Raises:
        ValueError: 比赛类型 / 奖项等级 / 年份非法
    """
    if competition_type not in CSP_AWARD_TYPE_SET:
        raise ValueError(f"competition_type 非法: {competition_type}")
    if award_level not in CSP_AWARD_LEVEL_SET:
        raise ValueError(f"award_level 非法: {award_level}")
    year = int(award_year)
    if not (2015 <= year <= 2030):
        raise ValueError(f"award_year 必须在 2015-2030: {year}")
    if actual_score is not None and not (0 <= int(actual_score) <= 600):
        raise ValueError("actual_score 必须在 0-600")

    conn = _get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO csp_awards (
                student_id, competition_type, award_level, award_year,
                actual_score, province, certificate_no, notes, recorded_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(student_id, competition_type, award_year, award_level) DO UPDATE SET
                actual_score = excluded.actual_score,
                province = excluded.province,
                certificate_no = excluded.certificate_no,
                notes = excluded.notes,
                recorded_by = excluded.recorded_by
            """,
            (
                int(student_id),
                competition_type,
                award_level,
                year,
                int(actual_score) if actual_score is not None else None,
                (province or None),
                (certificate_no or None),
                (notes or None),
                recorded_by,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_csp_awards(student_id: int) -> list[dict]:
    """学员所有 CSP/NOIP/NOI 奖项（按年份倒序）"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT id, competition_type, award_level, award_year,
                   actual_score, province, certificate_no, notes, recorded_by, created_at
            FROM csp_awards
            WHERE student_id = ?
            ORDER BY award_year DESC, id DESC
            """,
            (int(student_id),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_csp_award(award_id: int, student_id: int) -> bool:
    """删除奖项（仅本人或教练可删）"""
    conn = _get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM csp_awards WHERE id = ? AND student_id = ?",
            (int(award_id), int(student_id)),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_student_award_summary(student_id: int) -> dict:
    """汇总学员所有奖项（供家长版报告 + 段位图 + 强基判定用）

    Returns:
        {
            'csp_j_pre':  {'first': [(year, score, province), ...], 'second': [...], ...},
            'csp_j_final': {...},
            ...
            'noi_gold':   [{'year': 2024, 'score': 530, 'province': None}, ...],
            'best_overall': 'noi_gold' | 'csp_j_first' | 'gesp_l8' | None,
        }
    """
    awards = list_csp_awards(student_id)
    # 嵌套：competition_type → award_level → [records]
    summary: dict[str, dict[str, list]] = {}
    for a in awards:
        ctype = a["competition_type"]
        lvl = a["award_level"]
        summary.setdefault(ctype, {}).setdefault(lvl, []).append({
            "year": int(a["award_year"]),
            "score": a.get("actual_score"),
            "province": a.get("province"),
            "id": a["id"],
        })

    # best_overall = NOI 金 > NOI 银 > NOI 铜 > NOIP 一等 > CSP-S 复赛一等 > CSP-J 复赛一等
    best_priority = [
        ("noi_gold", 100),
        ("noi_silver", 90),
        ("noi_bronze", 80),
        ("noip_1", 70),
        ("csp_s_final", 60),
        ("csp_j_final", 50),
        ("csp_s_pre", 30),
        ("csp_j_pre", 20),
    ]
    best_overall = None
    best_year = None
    for ctype, prio in best_priority:
        if ctype in summary and summary[ctype]:
            best_overall = ctype
            # 取该 type 中最近一年
            all_years = []
            for lvl_records in summary[ctype].values():
                all_years.extend([r["year"] for r in lvl_records])
            if all_years:
                best_year = max(all_years)
            break

    # best_label = 中文（家长报告用）
    best_label = None
    if best_overall:
        type_label = dict(CSP_AWARD_TYPES).get(best_overall, best_overall)
        best_label = f"{type_label} {best_year}" if best_year else type_label

    return {
        "by_type": summary,
        "best_overall": best_overall,
        "best_year": best_year,
        "best_label": best_label,
        "total_awards": len(awards),
        "raw": awards,
    }


# ========== v3.5.3 学员画像 + 学段 GESP 视角（家长版报告用） ==========

# 学段判定（基于 grade 字段 · GRADES_REGISTRATION 编码）
STAGE_PRIMARY = "primary"        # 小学 1-6
STAGE_JUNIOR  = "junior"        # 初中 1-3
STAGE_SENIOR  = "senior"        # 高中 1-3
# v3.5.4: NOI 不再面向大学生，删除 STAGE_UNIVERSITY


def _grade_to_stage(grade: str | None) -> str:
    """根据 GRADES_REGISTRATION 编码判定学段

    v3.5.4 修订：NOI 不再面向大学生，未识别 grade 兜底为 senior
    """
    if not grade:
        return STAGE_SENIOR  # 默认高中
    g = str(grade).upper()
    if g.startswith("PRIMARY_"):
        return STAGE_PRIMARY
    if g.startswith("JUNIOR_"):
        return STAGE_JUNIOR
    if g.startswith("SENIOR_"):
        return STAGE_SENIOR
    if g.startswith("UNIV_"):
        # v3.5.4: 历史数据兼容 — UNIV_* 兜底为 senior
        return STAGE_SENIOR
    return STAGE_SENIOR


def _stage_recommendation(stage: str, age: int | None = None) -> dict:
    """根据学段返回 OI 路径建议（家长版报告用）

    小学 → GESP 视角（GESP 1-4 → 5-6 兴趣培养）
    初中 → GESP 5-7 + CSP-J 早规划
    高中 → CSP-S + NOIP + 强基 5 校

    v3.5.4 修订：NOI 不再面向大学生，删除 university 分支
    """
    if stage == STAGE_PRIMARY:
        return {
            "stage_label": "小学",
            "perspective": "GESP",
            "primary_focus": "GESP 1-4 级 · 兴趣培养 · 编程思维",
            "next_step": "GESP 5-6 级（如果通过 4 级 60+）",
            "csp_visible": False,
            "noi_visible": False,
            "policy_match_type": "tech_talent_junior",
            "guidance": "小学阶段以兴趣为主，可开始接触 GESP 1-2 级。3-4 年级后逐步挑战 GESP 3-4 级，"
                       "为初中 CSP-J 打基础。避免过早进入算法竞赛。",
        }
    if stage == STAGE_JUNIOR:
        return {
            "stage_label": "初中",
            "perspective": "GESP + CSP-J 早规划",
            "primary_focus": "GESP 5-7 级 · CSP-J 入门 · 当地科技特长生",
            "next_step": "GESP 7 级 80+ → 免 CSP-J 初赛（9 月）",
            "csp_visible": True,
            "noi_visible": False,
            "policy_match_type": "tech_talent_junior",
            "guidance": "初中是 OI 黄金期。GESP 7 级 80+ 或 8 级 60+ 可免 CSP-J 初赛。"
                       "同步关注当地科技特长生政策（小升初/初升高）。",
        }
    # senior / 其他兜底 — v3.5.4 统一视为高中
    return {
        "stage_label": "高中",
        "perspective": "CSP-S + 强基 5 校",
        "primary_focus": "GESP 8 级 · CSP-S · NOIP · 强基 5 校",
        "next_step": "GESP 8 级 80+ → 免 CSP-S 初赛 · NOIP 一等 → 强基破格",
        "csp_visible": True,
        "noi_visible": True,
        "policy_match_type": "qiangji_university",
        "guidance": "高中冲 CSP-S 一等 + NOIP 一等，NOI 金牌/银牌可破格入围清华北大强基计划。"
                   "GESP 8 级 80+ 可免 CSP-S 初赛。",
    }


def compute_student_profile(student_id: int) -> dict:
    """组装家长版报告所需的学员画像（聚合 students + awards + gesp + 段位图）

    Returns:
        {
            'student': {...},               # students 行
            'age': int | None,              # 实际年龄（基于 birth_date）
            'province': str,                # 省份
            'stage': 'primary'/'junior'/'senior'/'university',
            'stage_recommendation': {...},  # _stage_recommendation 输出
            'gesp_progress': {...},         # get_student_gesp_progress 输出
            'award_summary': {...},         # get_student_award_summary 输出
            'is_csp_age_eligible': bool,    # 12 岁年龄判断
        }
    """
    from gesp_estimator import is_csp_age_eligible

    student = get_student(student_id)
    if not student:
        return {}

    # 省份：students.province 字段 → fallback city→province 转换
    province = student.get("province") or ""
    if not province and student.get("city"):
        # 复用 web_app 里的 _city_to_province
        try:
            from web_app import _city_to_province
            province = _city_to_province(student.get("city")) or ""
        except Exception:
            province = ""

    # 年龄
    age = None
    bd = student.get("birth_date")
    bd_for_check = None
    if bd:
        try:
            bd_for_check = bd if isinstance(bd, date) else datetime.strptime(str(bd), "%Y-%m-%d").date()
            age = int((date.today() - bd_for_check).days / 365.25)
        except Exception:
            age = None
            bd_for_check = None

    # 学段 + 建议
    stage = _grade_to_stage(student.get("grade"))
    rec = _stage_recommendation(stage, age)

    return {
        "student": student,
        "age": age,
        "province": province,
        "stage": stage,
        "stage_recommendation": rec,
        "gesp_progress": get_student_gesp_progress(student_id),
        "award_summary": get_student_award_summary(student_id),
        "is_csp_age_eligible": bool(bd_for_check and is_csp_age_eligible(bd_for_check, date.today().year)),
    }


# ========== 视图查询 ==========


def list_gesp_exams(student_id: int) -> list[dict]:
    """学员所有 GESP 考试记录（join competitions）"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT g.id, g.registered_level, g.actual_score, g.passed,
                   g.can_skip_next, g.exempts_csp_j, g.exempts_csp_s,
                   g.certificate_no, g.notes, g.recorded_by, g.created_at,
                   g.award_year,
                   c.name AS exam_name, c.exam_date, c.code AS exam_code
            FROM gesp_exams g
            LEFT JOIN competitions c ON c.id = g.exam_id
            WHERE g.student_id = ?
            ORDER BY c.exam_date DESC, g.id DESC
            """,
            (int(student_id),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_student_gesp_progress(student_id: int) -> dict:
    """返回学员的 GESP 段位图 + 跳级建议 + 免初赛状态。
    供学员详情页渲染。

    next_eligible_level 用 students.gesp_next_eligible_level 字段（已重算过），
    而不是再次用 gesp_estimator 算一次 —— 避免"最近考试"≠"最高级别考试"的歧义。
    """
    student = get_student(student_id)
    if not student:
        return {}

    exams = list_gesp_exams(student_id)
    passed_levels = sorted({e["registered_level"] for e in exams if e.get("passed")})
    highest_score = {e["registered_level"]: e["actual_score"] for e in exams if e.get("passed")}

    bar = gesp_progress_bar(passed_levels, highest_score)
    # 直接用 DB 字段（由 _recompute_student_gesp_state 重算，按 id DESC 取最新插入）
    next_lv = int(student.get("gesp_next_eligible_level") or 1)
    return {
        "student": student,
        "exams": exams,
        "passed_levels": passed_levels,
        "highest_score": highest_score,
        "progress_bar": bar,
        "next_eligible_level": next_lv,
        "can_exempt_csp_j": bool(student.get("gesp_can_exempt_csp_j")),
        "can_exempt_csp_s": bool(student.get("gesp_can_exempt_csp_s")),
        "exemption_expiry": student.get("gesp_exemption_expiry"),
    }


# -- smoke test --
if __name__ == "__main__":
    import os
    if "TASK_DB_PATH" not in os.environ:
        os.environ["TASK_DB_PATH"] = str(ROOT / "tasks.db")

    print("[SMOKE] admin_students.py")

    # 1) 创建测试学员
    test_uid = "test_smoke_uid_001"
    existing = get_student_by_uid(test_uid)
    if existing:
        delete_student(existing["id"])
    sid = create_student(
        test_uid,
        real_name="测试学员甲",
        school="测试小学",
        grade="2024",
        is_minor=True,
    )
    assert sid > 0
    print(f"  [OK] create_student id={sid}")

    # 2) get / list
    s = get_student(sid)
    assert s and s["luogu_uid"] == test_uid
    print(f"  [OK] get_student real_name={s['real_name']}")
    all_students = list_students(limit=10)
    assert any(st["id"] == sid for st in all_students)
    print(f"  [OK] list_students 返回 {len(all_students)} 条")
    assert count_students() >= 1
    print(f"  [OK] count_students = {count_students()}")

    # 3) 找两个不同的 GESP 赛事（一个 L7-8 九月考 + 一个 L5-8 十二月考）
    #    实际场景：7级 9 月考过 → 12 月再考 8 级（不同 exam_id）
    conn = _get_conn()
    g7 = conn.execute(
        "SELECT id, code FROM competitions WHERE type='gesp' AND code LIKE '%L7-8%' LIMIT 1"
    ).fetchone()
    g8 = conn.execute(
        "SELECT id, code FROM competitions WHERE type='gesp' AND code LIKE '%L5-8%' LIMIT 1"
    ).fetchone()
    conn.close()
    assert g7 and g8 and g7["id"] != g8["id"], (
        f"需要 2 个不同 GESP 赛事，先跑 import_competitions.py。g7={g7} g8={g8}"
    )
    print(f"  [OK] 找到 GESP 7 级赛事 id={g7['id']} ({g7['code']})")
    print(f"  [OK] 找到 GESP 8 级赛事 id={g8['id']} ({g8['code']})")

    # 4) 录入 7 级 85 分 → 应触发 exempts_csp_j
    eid = add_gesp_exam(sid, g7["id"], 7, 85, recorded_by="smoke")
    print(f"  [OK] add_gesp_exam 7 级 85 分 id={eid}")
    s2 = get_student(sid)
    assert s2["gesp_can_exempt_csp_j"] == 1, f"未触发 csp_j 免: {s2}"
    assert s2["gesp_can_exempt_csp_s"] == 0
    print(f"  [OK] gesp_can_exempt_csp_j 自动置位")
    assert s2["gesp_highest_passed"] == 7
    assert s2["gesp_latest_score"] == 85
    assert s2["gesp_next_eligible_level"] == 8  # 60-89 → N+1
    print(f"  [OK] students 6 字段同步：highest={s2['gesp_highest_passed']} "
          f"latest={s2['gesp_latest_score']} next={s2['gesp_next_eligible_level']}")

    # 5) 录入 8 级 80 分 → 应触发 csp_j + csp_s 双免
    eid2 = add_gesp_exam(sid, g8["id"], 8, 80, recorded_by="smoke")
    s3 = get_student(sid)
    assert s3["gesp_can_exempt_csp_j"] == 1
    assert s3["gesp_can_exempt_csp_s"] == 1, f"未触发 csp_s 免: {s3}"
    assert s3["gesp_highest_passed"] == 8
    print(f"  [OK] 8 级 80 分 → csp_j + csp_s 双免")

    # 6) 段位图
    progress = get_student_gesp_progress(sid)
    assert "[7✦]" in progress["progress_bar"]  # 85 >= 80
    assert "[8✦]" in progress["progress_bar"]  # 80 >= 80
    print(f"  [OK] 段位图: {progress['progress_bar']}")

    # 7) 删除
    assert delete_student(sid)
    assert get_student(sid) is None
    print(f"  [OK] delete_student 级联删除")

    print("[OK] admin_students smoke test passed")
