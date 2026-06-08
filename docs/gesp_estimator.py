"""
gesp_estimator.py — v3.5 P0 stub

GESP 8 级 AI 估算 + 跳级算法 + 免初赛算法骨架。
本文件为占位实现，不依赖数据库/网络/AI API，便于 unit test。
完整实现见 docs/开发计划_v3.5.md § 1.2-1.3。

v3.5.1 业务规则（2026-06 锁定）：
  - GESP 最高等级 = 8（CCF 官方，1-8 共 8 级）
  - CSP 报名年龄门槛：当年 9 月 1 日前必须年满 12 周岁（CCF 官方）

用法（v3.5 完整版）：
    from gesp_estimator import (
        next_eligible_gesp_level,
        compute_exemptions,
        estimate_gesp_level,
        skip_level_decision_tree,
        is_csp_age_eligible,
    )
"""
from __future__ import annotations

from datetime import date, datetime

GESP_MIN_LEVEL = 1
GESP_MAX_LEVEL = 8          # CCF 官方：GESP 共 1-8 级
GESP_PASS_SCORE = 60
GESP_SKIP_SCORE = 90
CSP_MIN_AGE = 12             # CSP 报名年龄门槛：当年 9/1 前满 12 周岁
CSP_AGE_CUTOFF_MONTH = 9     # 9/1 作为 cutoff
CSP_AGE_CUTOFF_DAY = 1


# 1. 跳级算法
def next_eligible_gesp_level(last_exam: dict | None) -> int:
    """
    根据最近一次 GESP 考试结果，计算下次可报等级。

    规则（CCF 官方，v3.5 锁定）：
      - 首次：报 1 级
      - < 60：重考原级
      - 60-89：报 N+1
      - >= 90：报 N+2（封顶 8）

    Args:
        last_exam: {'registered_level': int, 'actual_score': int} 或 None

    Returns:
        下次可报等级（1-8）
    """
    if last_exam is None:
        return GESP_MIN_LEVEL
    if last_exam["actual_score"] < GESP_PASS_SCORE:
        return last_exam["registered_level"]
    if last_exam["actual_score"] >= GESP_SKIP_SCORE:
        return min(last_exam["registered_level"] + 2, GESP_MAX_LEVEL)
    return last_exam["registered_level"] + 1


# 2. 免初赛算法
GESP_EXEMPTION_MATRIX = {
    7: {80: ["csp_j"]},
    8: {60: ["csp_j"], 80: ["csp_j", "csp_s"]},
}


def compute_exemptions(gesp_level: int, score: int) -> list[str]:
    """
    根据 GESP 等级和分数，返回可免的初赛列表。

    Args:
        gesp_level: 学员 GESP 等级（7 或 8）
        score: 实际分数（0-100）

    Returns:
        ['csp_j', 'csp_s'] 子集
    """
    if gesp_level not in GESP_EXEMPTION_MATRIX:
        return []
    exemptions = []
    for threshold in sorted(GESP_EXEMPTION_MATRIX[gesp_level].keys(), reverse=True):
        if score >= threshold:
            exemptions.extend(GESP_EXEMPTION_MATRIX[gesp_level][threshold])
    return sorted(set(exemptions))


# 3. 段位图字符（v3.5 § 1.4）
def gesp_progress_bar(passed_levels: list[int], highest_score: dict[int, int] | None = None) -> str:
    """
    生成学员 GESP 1-8 级段位图。

    Args:
        passed_levels: 已通过等级列表，如 [1, 2, 3, 5]
        highest_score: {level: score} 已通过等级的最高分

    Returns:
        ASCII 段位图字符串
    """
    cells = []
    for lv in range(1, GESP_MAX_LEVEL + 1):
        if lv not in passed_levels:
            cells.append(f"[{lv} ]")
            continue
        score = (highest_score or {}).get(lv, 0)
        if score >= 80:
            cells.append(f"[{lv}✦]")  # 优秀通过（80+）
        else:
            cells.append(f"[{lv}★]")  # 通过
    return "──".join(cells)


# 4. AI 估算 GESP 等级（v3.5 P1，本版本为占位）
def estimate_gesp_level(luogu_passed_problems: int, by_difficulty: dict[str, int] | None = None) -> int:
    """
    基于洛谷已通过题数，估算 GESP 等级。

    **AI 估算仅供参考，UI 上必须显式 "AI 估算" 水印，建议真考验证。**

    占位规则（v3.5 完整版基于洛谷难度分布 + 知识点矩阵训练 ML 模型）：
      - 0-30 题 入门级 → 1-2 级
      - 30-80 题 普及- → 3-4 级
      - 80-150 题 普及 → 5-6 级
      - 150-300 题 提高- → 7 级
      - 300+ 题  提高  → 8 级
    """
    if luogu_passed_problems < 30:
        return 1
    if luogu_passed_problems < 80:
        return 3
    if luogu_passed_problems < 150:
        return 5
    if luogu_passed_problems < 300:
        return 7
    return 8


# 5. CSP 报名年龄判定（v3.5.1 新增）
def is_csp_age_eligible(birth_date: date | str | None, csp_year: int) -> dict:
    """
    判定学员在指定 CSP 年份是否满足年龄门槛：
      - 必须 **当年 9/1 前** 年满 12 周岁
      - 出生日期 ≤ (csp_year - 12) 年 9/1
      - 例：CSP 2026 → 必须出生于 2014-09-01（含）之前

    Args:
        birth_date: 出生日期（date / 'YYYY-MM-DD' / None）
        csp_year: CSP 年份（如 2026）

    Returns:
        {
            'eligible': bool,
            'cutoff_date': '2014-09-01',
            'age_at_cutoff': 12,
            'reason': '满足 / 差 N 天才满 12 / 出生日期缺失',
        }
    """
    if birth_date is None:
        return {
            "eligible": False,
            "cutoff_date": f"{csp_year - CSP_MIN_AGE}-09-01",
            "age_at_cutoff": CSP_MIN_AGE,
            "reason": "出生日期缺失，请联系管理员补充",
        }
    if isinstance(birth_date, str):
        try:
            bd = datetime.strptime(birth_date, "%Y-%m-%d").date()
        except ValueError:
            return {
                "eligible": False,
                "cutoff_date": f"{csp_year - CSP_MIN_AGE}-09-01",
                "age_at_cutoff": CSP_MIN_AGE,
                "reason": f"出生日期格式错误：{birth_date}",
            }
    else:
        bd = birth_date
    cutoff = date(csp_year - CSP_MIN_AGE, CSP_AGE_CUTOFF_MONTH, CSP_AGE_CUTOFF_DAY)
    if bd <= cutoff:
        return {
            "eligible": True,
            "cutoff_date": cutoff.isoformat(),
            "age_at_cutoff": CSP_MIN_AGE,
            "reason": f"出生于 {bd.isoformat()}，满足 CSP {csp_year} 年龄门槛",
        }
    days_short = (bd - cutoff).days
    return {
        "eligible": False,
        "cutoff_date": cutoff.isoformat(),
        "age_at_cutoff": CSP_MIN_AGE,
        "reason": f"出生于 {bd.isoformat()}，距 {cutoff.isoformat()} 还差 {days_short} 天才满 {CSP_MIN_AGE} 周岁",
    }


# 6. 跳级决策树（家长辅助）
def skip_level_decision_tree(
    current_level: int,
    last_score: int,
    target_level: int,
) -> dict:
    """
    给家长 3 个选项 + AI 推荐。

    Args:
        current_level: 已通过的最高 GESP 等级
        last_score: 最近一次该等级的实际分数
        target_level: 家长/学员想要报的等级

    Returns:
        {
          'options': [
            {'name': 'A. 报 N+1（稳）', 'pass_rate': 0.95, 'recommend': True},
            {'name': 'B. 报 N+2（跳 1 级）', 'pass_rate': 0.70, 'recommend': False},
            {'name': 'C. 报 N+3（跳 2 级）', 'pass_rate': 0.30, 'recommend': False},
          ],
          'ai_recommendation': 'B. 跳 1 级',
          'reasoning': '90+ 跳级机制已触发，建议跳 1 级节省 1 次考试'
        }
    """
    gap = target_level - current_level
    options = []
    pass_rates = {1: 0.95, 2: 0.70, 3: 0.30}
    for i, g in enumerate(["A", "B", "C"]):
        if gap - 1 == i:
            options.append({
                "name": f"{g}. 报 {current_level + i + 1}（{('稳' if i == 0 else f'跳 {i} 级')}）",
                "pass_rate": pass_rates.get(i + 1, 0.10),
                "recommend": (i == 1),  # 默认推荐跳 1 级
            })
    return {
        "options": options,
        "ai_recommendation": "B. 跳 1 级" if last_score >= 90 else "A. 报 N+1（稳）",
        "reasoning": (
            f"上次 {current_level} 级 {last_score} 分，"
            + ("已触发 90+ 跳级机制，建议跳 1 级节省 1 次考试" if last_score >= 90
               else "未达 90 分，建议稳扎稳打报 N+1")
        ),
    }


# -- 单测 / smoke test --
if __name__ == "__main__":
    # 跳级算法
    assert next_eligible_gesp_level(None) == 1
    assert next_eligible_gesp_level({"registered_level": 3, "actual_score": 95}) == 5
    assert next_eligible_gesp_level({"registered_level": 3, "actual_score": 50}) == 3
    assert next_eligible_gesp_level({"registered_level": 7, "actual_score": 92}) == 8  # 7+2=9 封顶 8
    assert next_eligible_gesp_level({"registered_level": 8, "actual_score": 95}) == 8  # 已是最高
    assert next_eligible_gesp_level({"registered_level": 3, "actual_score": 75}) == 4

    # 免初赛
    assert compute_exemptions(7, 80) == ["csp_j"]
    assert compute_exemptions(7, 95) == ["csp_j"]
    assert compute_exemptions(8, 60) == ["csp_j"]
    assert compute_exemptions(8, 80) == ["csp_j", "csp_s"]
    assert compute_exemptions(8, 59) == []
    assert compute_exemptions(6, 100) == []  # 6 级不在免初赛矩阵

    # 段位图
    bar = gesp_progress_bar([1, 2, 3, 5], {1: 95, 2: 70, 3: 65, 5: 92})
    assert "[1✦]" in bar  # 90+ 优秀
    assert "[2★]" in bar  # 60-89 普通通过
    assert "[4 ]" in bar  # 4 未通过
    assert "[5✦]" in bar  # 92 分优秀

    # AI 估算
    assert estimate_gesp_level(50) == 3
    assert estimate_gesp_level(200) == 7

    # 决策树
    tree = skip_level_decision_tree(3, 95, 5)
    assert "B" in tree["ai_recommendation"]

    # CSP 年龄门槛（v3.5.1 新增）
    # CSP 2026 → 出生 ≤ 2014-09-01 满足
    ok1 = is_csp_age_eligible("2014-09-01", 2026)
    assert ok1["eligible"] is True, f"9/1 当天及以前出生应满足：{ok1}"
    assert ok1["cutoff_date"] == "2014-09-01"

    ok2 = is_csp_age_eligible("2010-01-15", 2026)
    assert ok2["eligible"] is True, f"远期出生应满足：{ok2}"
    assert "满足" in ok2["reason"]

    ng1 = is_csp_age_eligible("2014-09-02", 2026)
    assert ng1["eligible"] is False, f"9/1 后 1 天应不满足：{ng1}"
    assert "还差 1 天" in ng1["reason"]

    ng2 = is_csp_age_eligible("2015-12-31", 2026)
    assert ng2["eligible"] is False, f"2015 年出生应不满足：{ng2}"
    assert "还差" in ng2["reason"]

    # date 对象入参
    from datetime import date as _date
    ok3 = is_csp_age_eligible(_date(2012, 6, 1), 2026)
    assert ok3["eligible"] is True

    # 缺数据
    no_bd = is_csp_age_eligible(None, 2026)
    assert no_bd["eligible"] is False
    assert "缺失" in no_bd["reason"]

    # 错格式
    bad = is_csp_age_eligible("not-a-date", 2026)
    assert bad["eligible"] is False
    assert "格式错误" in bad["reason"]

    # 跨年验证：2027 应为 ≤ 2015-09-01
    cross_year = is_csp_age_eligible("2015-09-01", 2027)
    assert cross_year["eligible"] is True
    assert cross_year["cutoff_date"] == "2015-09-01"

    print("[OK] gesp_estimator smoke test passed (含 v3.5.1 CSP 12 岁门槛)")
