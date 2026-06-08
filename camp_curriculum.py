"""
camp_curriculum.py — v3.5 Phase 3

冲刺营题库 + 进度跟踪 + 退费评估。

v3.5 §6.2 双 SKU 冲刺营：
  - popularize_camp  普及组冲刺营 ¥99/4 周（28 题，7 级 80+ / 8 级 60+ 目标）
  - improve_camp      提高组冲刺营 ¥299/8 周（56 题，8 级 80+ 目标）

业务规则（v3.5 §9 风险对冲 + §10 验收）：
  - 4 周后未达 80 分 → 退 50% + 延长 1 期（compile_camp_result 中触发）
  - 学员完成 1 题 → 记 camp_progress
  - 评估"达成" = 学完所有题 + 真考 80+
  - 每日推送 1 题（取 day = 当前 day_in_camp）
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from task_store import _get_conn  # noqa: E402

# ========== 冲刺营题库种子 ==========
# 注：v3.5 P0 用 stub 题目清单（覆盖 GESP 7/8 级常见考点）。
# 真实版本可对接洛谷题单 API。
# 难度 → GESP 等级映射复用 studymate_panel.DIFFICULTY_TO_GESP_LEVEL

POPULARIZE_CAMP_SEED: list[dict] = [
    # day 1-7: 基础算法（难度 1-3, GESP 1-4）
    {"day": 1, "pid": "B2001", "title": "入门级输入输出", "difficulty": 1, "gesp_level": 1, "topic": "入门"},
    {"day": 2, "pid": "B2002", "title": "Hello,World!", "difficulty": 1, "gesp_level": 1, "topic": "入门"},
    {"day": 3, "pid": "B2005", "title": "字符三角形", "difficulty": 2, "gesp_level": 2, "topic": "入门"},
    {"day": 4, "pid": "B2009", "title": "计算 (a+b)*c", "difficulty": 2, "gesp_level": 2, "topic": "入门"},
    {"day": 5, "pid": "P1421", "title": "小玉买文具", "difficulty": 2, "gesp_level": 2, "topic": "入门"},
    {"day": 6, "pid": "P1085", "title": "不高兴的津津", "difficulty": 3, "gesp_level": 3, "topic": "模拟"},
    {"day": 7, "pid": "P1046", "title": "【NOIP2005 普及组】校门外的树", "difficulty": 3, "gesp_level": 3, "topic": "模拟"},
    # day 8-14: 普及-（难度 3-4, GESP 4）
    {"day": 8, "pid": "P1055", "title": "ISBN 号码", "difficulty": 3, "gesp_level": 4, "topic": "字符串"},
    {"day": 9, "pid": "P1003", "title": "铺地毯", "difficulty": 3, "gesp_level": 4, "topic": "枚举"},
    {"day": 10, "pid": "P1067", "title": "多项式输出", "difficulty": 3, "gesp_level": 4, "topic": "模拟"},
    {"day": 11, "pid": "P1087", "title": "FBI 树", "difficulty": 4, "gesp_level": 4, "topic": "递归"},
    {"day": 12, "pid": "P1093", "title": "奖学金", "difficulty": 4, "gesp_level": 4, "topic": "排序"},
    {"day": 13, "pid": "P1056", "title": "排座椅", "difficulty": 4, "gesp_level": 4, "topic": "贪心"},
    {"day": 14, "pid": "P1080", "title": "国王游戏", "difficulty": 4, "gesp_level": 4, "topic": "贪心"},
    # day 15-21: 普及（难度 4-5, GESP 5）
    {"day": 15, "pid": "P1162", "title": "填涂颜色", "difficulty": 4, "gesp_level": 5, "topic": "BFS"},
    {"day": 16, "pid": "P1141", "title": "01 迷宫", "difficulty": 4, "gesp_level": 5, "topic": "BFS"},
    {"day": 17, "pid": "P1031", "title": "均分纸牌", "difficulty": 4, "gesp_level": 5, "topic": "贪心"},
    {"day": 18, "pid": "P1091", "title": "合唱队形", "difficulty": 4, "gesp_level": 5, "topic": "DP"},
    {"day": 19, "pid": "P1049", "title": "装箱问题", "difficulty": 4, "gesp_level": 5, "topic": "DP"},
    {"day": 20, "pid": "P1060", "title": "开心的金明", "difficulty": 5, "gesp_level": 5, "topic": "DP"},
    {"day": 21, "pid": "P1103", "title": "书本整理", "difficulty": 5, "gesp_level": 5, "topic": "DP"},
    # day 22-28: 普及+（难度 5-6, GESP 6-7）—— 冲刺 7 级 80+
    {"day": 22, "pid": "P1024", "title": "一元三次方程求解", "difficulty": 5, "gesp_level": 6, "topic": "数学"},
    {"day": 23, "pid": "P1025", "title": "数的划分", "difficulty": 5, "gesp_level": 6, "topic": "递推"},
    {"day": 24, "pid": "P1057", "title": "传球游戏", "difficulty": 5, "gesp_level": 6, "topic": "DP"},
    {"day": 25, "pid": "P1063", "title": "能量项链", "difficulty": 5, "gesp_level": 6, "topic": "DP"},
    {"day": 26, "pid": "P1123", "title": "取数游戏", "difficulty": 5, "gesp_level": 7, "topic": "DFS"},
    {"day": 27, "pid": "P1219", "title": "八皇后", "difficulty": 5, "gesp_level": 7, "topic": "DFS"},
    {"day": 28, "pid": "P1004", "title": "方格取数", "difficulty": 6, "gesp_level": 7, "topic": "DP"},
]

IMPROVE_CAMP_SEED: list[dict] = [
    # 前 28 题复用 popularize,加 28 题 GESP 7-8 专项
    *POPULARIZE_CAMP_SEED,
    # day 29-42: 提高（难度 6-7, GESP 7-8）—— 冲刺 8 级 80+
    {"day": 29, "pid": "P1006", "title": "传纸条", "difficulty": 6, "gesp_level": 7, "topic": "DP"},
    {"day": 30, "pid": "P1373", "title": "小 a 和 uim 大逃亡", "difficulty": 6, "gesp_level": 7, "topic": "DP"},
    {"day": 31, "pid": "P1019", "title": "单词接龙", "difficulty": 6, "gesp_level": 7, "topic": "DFS"},
    {"day": 32, "pid": "P1027", "title": "Car 的旅行路线", "difficulty": 6, "gesp_level": 7, "topic": "Floyd"},
    {"day": 33, "pid": "P1058", "title": "立体图", "difficulty": 6, "gesp_level": 7, "topic": "模拟"},
    {"day": 34, "pid": "P1069", "title": "细胞分裂", "difficulty": 6, "gesp_level": 7, "topic": "数论"},
    {"day": 35, "pid": "P1092", "title": "虫食算", "difficulty": 6, "gesp_level": 7, "topic": "搜索"},
    {"day": 36, "pid": "P1094", "title": "纪念品分组", "difficulty": 5, "gesp_level": 7, "topic": "贪心"},
    {"day": 37, "pid": "P1125", "title": "笨小猴", "difficulty": 5, "gesp_level": 7, "topic": "字符串"},
    {"day": 38, "pid": "P1199", "title": "三国游戏", "difficulty": 6, "gesp_level": 7, "topic": "贪心"},
    {"day": 39, "pid": "P1508", "title": "Likecloud-吃、吃、吃", "difficulty": 6, "gesp_level": 7, "topic": "DP"},
    {"day": 40, "pid": "P1510", "title": "精卫填海", "difficulty": 6, "gesp_level": 7, "topic": "二分"},
    {"day": 41, "pid": "P1605", "title": "迷宫", "difficulty": 6, "gesp_level": 7, "topic": "DFS"},
    {"day": 42, "pid": "P1644", "title": "跳马问题", "difficulty": 6, "gesp_level": 7, "topic": "DFS"},
    # day 43-56: 提高+/省选（难度 7, GESP 8 专项）
    {"day": 43, "pid": "P1115", "title": "最大子段和", "difficulty": 5, "gesp_level": 8, "topic": "DP"},
    {"day": 44, "pid": "P1226", "title": "快速幂", "difficulty": 5, "gesp_level": 8, "topic": "数论"},
    {"day": 45, "pid": "P1352", "title": "没有上司的舞会", "difficulty": 6, "gesp_level": 8, "topic": "树形DP"},
    {"day": 46, "pid": "P1525", "title": "关押罪犯", "difficulty": 6, "gesp_level": 8, "topic": "并查集"},
    {"day": 47, "pid": "P1541", "title": "乌龟棋", "difficulty": 6, "gesp_level": 8, "topic": "DP"},
    {"day": 48, "pid": "P1629", "title": "邮递员送信", "difficulty": 6, "gesp_level": 8, "topic": "图论"},
    {"day": 49, "pid": "P1886", "title": "滑动窗口", "difficulty": 6, "gesp_level": 8, "topic": "单调队列"},
    {"day": 50, "pid": "P1969", "title": "积木大赛", "difficulty": 6, "gesp_level": 8, "topic": "贪心"},
    {"day": 51, "pid": "P2014", "title": "CTSC1997 选课", "difficulty": 7, "gesp_level": 8, "topic": "树形DP"},
    {"day": 52, "pid": "P2272", "title": "最大半连通子图", "difficulty": 7, "gesp_level": 8, "topic": "图论"},
    {"day": 53, "pid": "P2336", "title": "喵星球上的点名", "difficulty": 7, "gesp_level": 8, "topic": "后缀数组"},
    {"day": 54, "pid": "P2602", "title": "数字计数", "difficulty": 7, "gesp_level": 8, "topic": "数位DP"},
    {"day": 55, "pid": "P2822", "title": "组合数问题", "difficulty": 7, "gesp_level": 8, "topic": "数学"},
    {"day": 56, "pid": "P3809", "title": "后缀排序", "difficulty": 7, "gesp_level": 8, "topic": "字符串"},
]

CAMP_SEEDS = {
    "popularize_camp": POPULARIZE_CAMP_SEED,
    "improve_camp": IMPROVE_CAMP_SEED,
}

# 冲刺营目标 GESP 等级（用于评估"达成"）
CAMP_TARGET_GESP = {
    "popularize_camp": 7,  # 7 级 80+ / 8 级 60+ → 9 月 CSP-J 免初赛
    "improve_camp": 8,     # 8 级 80+ → 9 月 CSP-S 免初赛
}

CAMP_DURATION_DAYS = {
    "popularize_camp": 28,  # 4 周
    "improve_camp": 56,     # 8 周
}


# ========== 题库种子 ==========

def seed_camp_curriculum(sku: str, *, force: bool = False) -> int:
    """
    把 CAMP_SEEDS[sku] 灌入 camp_problems。
    force=True → 先清后插；默认跳过已存在。
    """
    if sku not in CAMP_SEEDS:
        raise ValueError(f"未知 SKU: {sku}")
    seed = CAMP_SEEDS[sku]

    conn = _get_conn()
    try:
        if force:
            conn.execute("DELETE FROM camp_problems WHERE sku = ?", (sku,))
        n = 0
        for row in seed:
            try:
                conn.execute(
                    """
                    INSERT INTO camp_problems
                        (sku, day, pid, title, difficulty, gesp_level, topic)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (sku, int(row["day"]), row["pid"], row["title"],
                     int(row["difficulty"]), int(row["gesp_level"]),
                     row.get("topic", "")),
                )
                n += 1
            except Exception:
                # UNIQUE 冲突 → 跳过
                pass
        conn.commit()
    finally:
        conn.close()
    return n


def list_camp_problems(sku: str) -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM camp_problems WHERE sku = ? ORDER BY day ASC",
            (sku,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ========== 学员冲刺营进度 ==========

def assign_camp_to_student(student_id: int, activation_id: int) -> int:
    """
    学员兑换冲刺营后，初始化进度（每题 1 行，submitted=0）。
    返回插入的进度行数。
    """
    conn = _get_conn()
    try:
        # 查 sku
        ac = conn.execute(
            "SELECT sku FROM activation_codes WHERE id = ?", (int(activation_id),)
        ).fetchone()
        if not ac:
            raise ValueError(f"activation_id {activation_id} 不存在")
        sku = ac["sku"]
        # 学员是否已存在该 activation 的进度
        existing = conn.execute(
            "SELECT COUNT(*) FROM camp_progress WHERE activation_id = ?", (int(activation_id),)
        ).fetchone()[0]
        if existing > 0:
            return int(existing)
        # 拉所有题
        problems = conn.execute(
            "SELECT id FROM camp_problems WHERE sku = ?", (sku,)
        ).fetchall()
        rows = [
            (int(student_id), int(activation_id), sku, int(p["id"]))
            for p in problems
        ]
        if not rows:
            return 0
        conn.executemany(
            """
            INSERT INTO camp_progress
                (student_id, activation_id, sku, problem_id, submitted, score)
            VALUES (?, ?, ?, ?, 0, NULL)
            """,
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def mark_problem_done(student_id: int, problem_id: int, score: int) -> dict | None:
    """学员提交 1 题，更新 camp_progress"""
    conn = _get_conn()
    try:
        cur = conn.execute(
            """
            UPDATE camp_progress
            SET submitted = 1, score = ?, submitted_at = ?
            WHERE student_id = ? AND problem_id = ? AND submitted = 0
            """,
            (int(score), datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             int(student_id), int(problem_id)),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        return {"student_id": int(student_id), "problem_id": int(problem_id), "score": int(score)}
    finally:
        conn.close()


def get_camp_progress(student_id: int, activation_id: int) -> dict:
    """学员在某个 activation 下的冲刺营进度"""
    conn = _get_conn()
    try:
        # 基础信息
        ac = conn.execute(
            "SELECT * FROM activation_codes WHERE id = ? AND student_id = ?",
            (int(activation_id), int(student_id)),
        ).fetchone()
        if not ac:
            return {"error": f"activation_id {activation_id} 与学员 {student_id} 不匹配"}
        ac_d = dict(ac)

        # 进度统计
        rows = conn.execute(
            """
            SELECT cp.id AS progress_id, cp.submitted, cp.score, cp.submitted_at,
                   p.id AS problem_id, p.day, p.pid, p.title,
                   p.difficulty, p.gesp_level, p.topic
            FROM camp_progress cp
            JOIN camp_problems p ON p.id = cp.problem_id
            WHERE cp.activation_id = ?
            ORDER BY p.day ASC
            """,
            (int(activation_id),),
        ).fetchall()
        items = [dict(r) for r in rows]
        total = len(items)
        submitted = sum(1 for it in items if it["submitted"])
        done_pct = (submitted / total * 100) if total else 0.0
        return {
            "activation": ac_d,
            "items": items,
            "total": total,
            "submitted": submitted,
            "done_pct": round(done_pct, 1),
            "current_day": _current_day_in_camp(ac_d.get("redeemed_at")),
        }
    finally:
        conn.close()


def _current_day_in_camp(redeemed_at: str | None) -> int:
    """学员兑换后第几天（1-based）"""
    if not redeemed_at:
        return 0
    try:
        rd = datetime.strptime(str(redeemed_at)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0
    elapsed = (datetime.now() - rd).days
    return max(1, min(elapsed + 1, 999))


def get_today_problem(student_id: int, activation_id: int) -> dict | None:
    """取学员今天应做的题（day = current_day）"""
    progress = get_camp_progress(student_id, activation_id)
    if "error" in progress:
        return None
    target_day = progress["current_day"]
    if target_day <= 0:
        return None
    for it in progress["items"]:
        if int(it["day"]) == target_day:
            return it
    # 超过题数 → 取最后一题
    if progress["items"]:
        return progress["items"][-1]
    return None


# ========== 冲刺营达成评估 + 退费建议 ==========

def evaluate_camp_completion(student_id: int, activation_id: int) -> dict:
    """
    评估冲刺营结果：
      - 完成度 = 提交数 / 总数
      - 真考 80+ 检查：从 gesp_exams 拉最近一次 GESP 真考（如果 gesp_level >= 目标且 score >= 80）
      - 达成 = 完成度 >= 90% AND 真考 80+
      - 未达成且已结营 → 触发退费建议（v3.5 §9 风险对冲）
    """
    progress = get_camp_progress(student_id, activation_id)
    if "error" in progress:
        return progress
    ac = progress["activation"]
    sku = ac["sku"]
    target_level = CAMP_TARGET_GESP.get(sku, 7)
    duration = CAMP_DURATION_DAYS.get(sku, 28)
    days_in = progress["current_day"]

    # 真考成绩（admin_students.add_gesp_exam 入库）
    from admin_students import list_gesp_exams
    exams = list_gesp_exams(int(student_id))
    relevant = [e for e in exams if int(e.get("registered_level") or 0) >= int(target_level)]
    passed_exam = next((e for e in relevant if int(e.get("actual_score") or 0) >= 80), None)
    exam_score = int(passed_exam["actual_score"]) if passed_exam else None

    completion_pct = float(progress["done_pct"])
    achieved = (
        completion_pct >= 90.0
        and exam_score is not None
        and exam_score >= 80
    )

    # 退费建议
    refund_recommended = False
    refund_reason = None
    if days_in >= duration and not achieved:
        refund_recommended = True
        if completion_pct < 90.0:
            refund_reason = f"完成度仅 {completion_pct:.0f}%（要求 ≥ 90%）"
        elif exam_score is None:
            refund_reason = f"未在目标 GESP {target_level} 级真考中达 80+"
        else:
            refund_reason = f"真考 {exam_score} 分（要求 ≥ 80）"

    return {
        "student_id": int(student_id),
        "activation_id": int(activation_id),
        "sku": sku,
        "target_gesp_level": int(target_level),
        "camp_duration_days": int(duration),
        "days_in_camp": int(days_in),
        "completion_pct": completion_pct,
        "submitted": int(progress["submitted"]),
        "total": int(progress["total"]),
        "exam_score": exam_score,
        "passed_exam_at_level": int(passed_exam["registered_level"]) if passed_exam else None,
        "achieved": bool(achieved),
        "refund_recommended": bool(refund_recommended),
        "refund_reason": refund_reason,
    }


# ========== 班期整体达成率（v3.5 §10 验收） ==========

def camp_pass_rate(*, sku: str, min_score: int = 80) -> dict:
    """
    冲刺营整体通过率：所有已兑换学员中"达成"的比例。
    用于 v3.5 §10 验收"≥ 30% 学员达成"。
    """
    from activation_codes import is_code_active
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT id, student_id FROM activation_codes
            WHERE sku = ? AND redeemed_at IS NOT NULL AND student_id IS NOT NULL
            """,
            (sku,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return {"sku": sku, "total_enrolled": 0, "achieved": 0, "pass_rate_pct": 0.0}
    achieved = 0
    for r in rows:
        if not is_code_active(r["code"]) if hasattr(r, "code") else True:
            pass  # 已过期也可能"达成"，不剔除
        ev = evaluate_camp_completion(int(r["student_id"]), int(r["id"]))
        if ev.get("achieved"):
            achieved += 1
    total = len(rows)
    return {
        "sku": sku,
        "total_enrolled": total,
        "achieved": achieved,
        "pass_rate_pct": round(achieved / total * 100, 1) if total else 0.0,
    }


# ========== 政策日历数据水印（v3.5 §9 风险对冲） ==========

# 政策事件种子
POLICY_EVENTS_SEED = [
    {
        "event_code": "QJ-2026-NOTICE",
        "name": "2026 强基计划简章发布",
        "category": "高校招生",
        "event_date": "2026-03-25",
        "target_audience": "高一/高二",
    },
    {
        "event_code": "GESP-2026-03",
        "name": "GESP 2026 年 3 月认证",
        "category": "GESP",
        "event_date": "2026-03-22",
        "target_audience": "全部",
    },
    {
        "event_code": "GESP-2026-06",
        "name": "GESP 2026 年 6 月认证",
        "category": "GESP",
        "event_date": "2026-06-21",
        "target_audience": "全部",
    },
    {
        "event_code": "CSP-J-2026-09",
        "name": "CSP-J 2026 初赛",
        "category": "CSP",
        "event_date": "2026-09-19",
        "target_audience": "普及组",
    },
    {
        "event_code": "CSP-S-2026-09",
        "name": "CSP-S 2026 初赛",
        "category": "CSP",
        "event_date": "2026-09-19",
        "target_audience": "提高组",
    },
    {
        "event_code": "ZHK-2026",
        "name": "2026 中考",
        "category": "中考",
        "event_date": "2026-06-13",
        "target_audience": "初三",
    },
    {
        "event_code": "GK-2026",
        "name": "2026 高考",
        "category": "高考",
        "event_date": "2026-06-07",
        "target_audience": "高三",
    },
]


def seed_policy_events(force: bool = False, verbose: bool = False) -> int:
    """灌入政策事件种子（v3.5 §9 风险对冲：UI 显式"最后更新于"水印）"""
    conn = _get_conn()
    try:
        # 检查表是否存在
        try:
            conn.execute("SELECT 1 FROM policy_events LIMIT 1").fetchone()
        except Exception:
            # 表不存在 → 自动创建
            conn.execute("""
                CREATE TABLE IF NOT EXISTS policy_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_code TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    category TEXT,
                    event_date DATE NOT NULL,
                    target_audience TEXT,
                    last_updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
        if force:
            conn.execute("DELETE FROM policy_events")
        n = 0
        for ev in POLICY_EVENTS_SEED:
            try:
                conn.execute(
                    """
                    INSERT INTO policy_events
                        (event_code, name, category, event_date, target_audience, data_year, last_updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (ev["event_code"], ev["name"], ev["category"],
                     ev["event_date"], ev.get("target_audience"),
                     2026, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
                n += 1
            except Exception as e:
                # UNIQUE 冲突 = 已存在 → 静默跳过（verbose=True 才打印）
                if verbose:
                    print(f"     WARN: skip {ev.get('event_code')}: {e}")
        conn.commit()
    finally:
        conn.close()
    return n


def get_policy_events_last_updated() -> str | None:
    """拉取 policy_events 表最近更新时间（UI 水印）"""
    conn = _get_conn()
    try:
        row = conn.execute("SELECT MAX(last_updated_at) AS lu FROM policy_events").fetchone()
        if not row or not row["lu"]:
            return None
        return str(row["lu"])
    except Exception:
        return None
    finally:
        conn.close()


# -- smoke test --
if __name__ == "__main__":
    import os
    if "TASK_DB_PATH" not in os.environ:
        os.environ["TASK_DB_PATH"] = str(ROOT / "tasks.db")

    print("[SMOKE] camp_curriculum.py")

    # 0) 准备学员 + 兑换
    import admin_students
    import activation_codes

    test_uid = "test_camp_smoke_001"
    existing = admin_students.get_student_by_uid(test_uid)
    if existing:
        admin_students.delete_student(existing["id"])
    sid = admin_students.create_student(test_uid, real_name="冲刺营测试", school="测试小学", grade="2024")

    # 1) 种子题库
    n1 = seed_camp_curriculum("popularize_camp")
    n2 = seed_camp_curriculum("improve_camp")
    print(f"  [OK] seed popularize: {n1} 题, improve: {n2} 题")

    # 2) list_camp_problems
    pop = list_camp_problems("popularize_camp")
    imp = list_camp_problems("improve_camp")
    assert len(pop) == 28
    assert len(imp) == 56
    print(f"  [OK] 普及组题库 {len(pop)} 题 / 提高组题库 {len(imp)} 题")

    # 3) 兑换冲刺营
    code = activation_codes.generate_codes("popularize_camp", count=1, student_id=int(sid))[0]
    ac = activation_codes.redeem_code(code, int(sid))
    print(f"  [OK] 兑换冲刺营: code={code}, activation_id={ac['id']}")

    # 4) 分配进度
    n_prog = assign_camp_to_student(int(sid), int(ac["id"]))
    assert n_prog == 28
    print(f"  [OK] 分配进度: {n_prog} 行")

    # 5) 标记前 3 题完成
    progress = get_camp_progress(int(sid), int(ac["id"]))
    # 正确调用
    for it in progress["items"][:3]:
        mark_problem_done(int(sid), int(it["problem_id"]), 100)
    progress2 = get_camp_progress(int(sid), int(ac["id"]))
    assert progress2["submitted"] >= 3
    print(f"  [OK] 标记 3 题完成 → submitted={progress2['submitted']}/28 ({progress2['done_pct']}%)")

    # 6) get_today_problem
    today = get_today_problem(int(sid), int(ac["id"]))
    assert today and today["day"] >= 1
    print(f"  [OK] 今日题目: day={today['day']} {today['pid']} {today['title'][:20]}")

    # 7) 评估：未完成 → 不达成
    ev = evaluate_camp_completion(int(sid), int(ac["id"]))
    assert not ev["achieved"]
    assert ev["submitted"] == 3
    print(f"  [OK] 评估: 完成 {ev['completion_pct']}% (未达成)")

    # 8) 模拟 GESP 7 级 85 分真考
    conn = _get_conn()
    g7 = conn.execute(
        "SELECT id FROM competitions WHERE type='gesp' AND code LIKE '%L7-8%' LIMIT 1"
    ).fetchone()
    conn.close()
    admin_students.add_gesp_exam(int(sid), int(g7["id"]), 7, 85, recorded_by="smoke")
    ev2 = evaluate_camp_completion(int(sid), int(ac["id"]))
    assert ev2["exam_score"] == 85
    assert ev2["achieved"] is False  # 完成度 3/28 = 10.7% < 90%
    print(f"  [OK] 真考 85 分后: exam_score={ev2['exam_score']}, achieved={ev2['achieved']}")

    # 9) 全部完成 + 真考 80+ → 达成
    for it in progress["items"][3:]:
        mark_problem_done(int(sid), int(it["problem_id"]), 100)
    ev3 = evaluate_camp_completion(int(sid), int(ac["id"]))
    assert ev3["achieved"] is True
    print(f"  [OK] 全部完成 + 真考 85: achieved={ev3['achieved']} (目标达成)")

    # 10) camp_pass_rate
    rate = camp_pass_rate(sku="popularize_camp")
    assert rate["achieved"] >= 1
    print(f"  [OK] 班期通过率: {rate['achieved']}/{rate['total_enrolled']} = {rate['pass_rate_pct']}%")

    # 11) 政策事件 + 水印
    n3 = seed_policy_events(force=True)
    last_updated = get_policy_events_last_updated()
    print(f"  [OK] 政策事件种子: {n3} 条, last_updated={last_updated}")
    assert last_updated, f"政策事件水印仍为空，请检查表结构"

    # 清理
    admin_students.delete_student(int(sid))
    print(f"  [OK] 清理学员 id={sid}")

    print("[OK] camp_curriculum smoke test passed")
