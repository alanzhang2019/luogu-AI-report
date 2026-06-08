"""
洛谷提交行为深度分析模块
基于用户提交记录进行行为模式、作息规律、AC率等分析
"""

from collections import Counter, defaultdict
from datetime import datetime, timedelta
import math
from typing import Any


def analyze_submission_behavior(records: list[dict[str, Any]]) -> dict[str, Any]:
    """
    对用户的提交记录进行深度行为分析
    records: 从 get_record_list 获取的原始记录列表
    """
    if not records:
        return {"error": "无提交记录"}

    total_records = len(records)

    # 状态统计
    status_counter = Counter()
    pid_records = defaultdict(list)  # 每道题的所有提交
    hourly_distribution = Counter()  # 小时分布
    weekday_distribution = Counter()  # 星期分布
    daily_submit_count = Counter()   # 每日提交数
    daily_ac_count = Counter()       # 每日AC数

    # 遍历所有记录
    for r in records:
        status = r.get("status", 0)
        pid = r.get("problem", {}).get("pid", "")
        submit_time = r.get("submitTime", 0)
        score = r.get("score", 0)

        status_counter[status] += 1

        if pid:
            pid_records[pid].append(r)

        if submit_time:
            dt = datetime.fromtimestamp(submit_time)
            hourly_distribution[dt.hour] += 1
            weekday_distribution[dt.weekday()] += 1
            date_key = dt.strftime("%Y-%m-%d")
            daily_submit_count[date_key] += 1
            if status == 12:  # 12 = AC
                daily_ac_count[date_key] += 1

    # ========== 1. AC率分析 ==========
    ac_count = status_counter.get(12, 0)
    ac_rate = ac_count / total_records if total_records > 0 else 0

    # 一次AC率：统计每道题首次提交即AC的比例
    first_try_ac = 0
    total_tried_pids = 0
    max_submit_pid = None
    max_submit_count = 0
    stuck_pids = []  # 卡题（提交>=3次且最终未AC）
    long_time_pids = []  # 长耗时题
    ac_submit_distribution = Counter() # 记录每次 AC 之前提交的次数

    for pid, submits in pid_records.items():
        total_tried_pids += 1
        submits_sorted = sorted(submits, key=lambda x: x.get("submitTime", 0))

        # 找到第一次 AC 的提交
        ac_idx = -1
        for i, s in enumerate(submits_sorted):
            if s.get("status") == 12:
                ac_idx = i
                break
        
        if ac_idx != -1:
            ac_submit_distribution[ac_idx + 1] += 1
            if ac_idx == 0:
                first_try_ac += 1

        # 卡题：三次及以上提交且最终未通过
        has_ac = any(s.get("status") == 12 for s in submits)
        if len(submits) >= 3 and not has_ac:
            stuck_pids.append({
                "pid": pid,
                "title": submits[0].get("problem", {}).get("title", ""),
                "submit_count": len(submits),
                "final_status": "未AC",
            })

        if len(submits) > max_submit_count:
            max_submit_count = len(submits)
            max_submit_pid = pid

    first_try_ac_rate = first_try_ac / total_tried_pids if total_tried_pids > 0 else 0
    stuck_pids.sort(key=lambda x: x["submit_count"], reverse=True)

    # ========== 2. 作息规律分析 ==========
    # 时段分类
    time_slots = {
        "凌晨 (0-5点)": sum(hourly_distribution.get(h, 0) for h in range(0, 6)),
        "早晨 (6-9点)": sum(hourly_distribution.get(h, 0) for h in range(6, 10)),
        "上午 (9-12点)": sum(hourly_distribution.get(h, 0) for h in range(10, 13)),
        "下午 (13-17点)": sum(hourly_distribution.get(h, 0) for h in range(13, 18)),
        "傍晚 (17-20点)": sum(hourly_distribution.get(h, 0) for h in range(17, 21)),
        "晚上 (20-23点)": sum(hourly_distribution.get(h, 0) for h in range(20, 24)),
    }

    peak_hour = max(hourly_distribution.keys(), key=lambda h: hourly_distribution[h]) if hourly_distribution else None
    peak_hour_count = hourly_distribution[peak_hour] if peak_hour is not None else 0

    # 星期分类
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    weekday_dist_named = {weekday_names[i]: weekday_distribution.get(i, 0) for i in range(7)}
    weekend_count = weekday_distribution.get(5, 0) + weekday_distribution.get(6, 0)
    weekday_count = sum(weekday_distribution.get(i, 0) for i in range(5))

    # ========== 3. 活跃度分析 ==========
    active_days = len(daily_submit_count)
    total_days_span = 1
    if daily_submit_count:
        dates = sorted(daily_submit_count.keys())
        first_date = datetime.strptime(dates[0], "%Y-%m-%d")
        last_date = datetime.strptime(dates[-1], "%Y-%m-%d")
        total_days_span = max(1, (last_date - first_date).days + 1)

    active_rate = active_days / total_days_span if total_days_span > 0 else 0

    # 单日最高提交
    max_daily_submit = max(daily_submit_count.values()) if daily_submit_count else 0
    max_daily_date = max(daily_submit_count.keys(), key=lambda d: daily_submit_count[d]) if daily_submit_count else None

    # 连续训练天数
    consecutive_days = _max_consecutive_days(set(daily_submit_count.keys()))

    # ========== 4. 编译错误分析 ==========
    ce_count = status_counter.get(3, 0) + status_counter.get(4, 0)  # CE 相关状态码
    ce_rate = ce_count / total_records if total_records > 0 else 0

    # ========== 5. 调试耐心分析 ==========
    wa_resubmit_intervals = []
    for pid, submits in pid_records.items():
        submits_sorted = sorted(submits, key=lambda x: x.get("submitTime", 0))
        for i in range(1, len(submits_sorted)):
            prev = submits_sorted[i - 1]
            curr = submits_sorted[i]
            # 如果前一次不是AC，计算间隔
            if prev.get("status") != 12:
                interval = curr.get("submitTime", 0) - prev.get("submitTime", 0)
                if 0 < interval < 3600:  # 只统计1小时内的重交
                    wa_resubmit_intervals.append(interval)

    median_resubmit_interval = _median(wa_resubmit_intervals) if wa_resubmit_intervals else None
    quick_resubmit_rate = sum(1 for x in wa_resubmit_intervals if x < 60) / len(wa_resubmit_intervals) if wa_resubmit_intervals else 0

    result = {
        "total_records": total_records,
        "total_unique_problems": total_tried_pids,
        "ac_count": ac_count,
        "ac_rate": round(ac_rate, 3),
        "first_try_ac_rate": round(first_try_ac_rate, 3),
        "ce_count": ce_count,
        "ce_rate": round(ce_rate, 3),
        "status_distribution": dict(status_counter),
        "hourly_distribution": dict(hourly_distribution),
        "time_slot_distribution": time_slots,
        "peak_hour": peak_hour,
        "peak_hour_count": peak_hour_count,
        "weekday_distribution": weekday_dist_named,
        "weekend_vs_weekday": {"周末": weekend_count, "工作日": weekday_count},
        "active_days": active_days,
        "total_days_span": total_days_span,
        "active_rate": round(active_rate, 3),
        "max_daily_submits": max_daily_submit,
        "max_daily_date": max_daily_date,
        "max_consecutive_days": consecutive_days,
        "stuck_problems": stuck_pids[:10],  # TOP10 死磕题
        "max_submit_single_problem": {"pid": max_submit_pid, "count": max_submit_count},
        "debug_patience": {
            "median_resubmit_interval_seconds": median_resubmit_interval,
            "quick_resubmit_under_60s_rate": round(quick_resubmit_rate, 3),
        },
        "ac_submit_distribution": dict(ac_submit_distribution),
    }

    result["personality_scores"] = compute_personality_scores(result)
    return result


def _max_consecutive_days(date_strings: set[str]) -> int:
    """计算最大连续训练天数"""
    if not date_strings:
        return 0
    dates = sorted(datetime.strptime(d, "%Y-%m-%d").date() for d in date_strings)
    max_streak = 1
    current = 1
    for i in range(1, len(dates)):
        if (dates[i] - dates[i - 1]).days == 1:
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 1
    return max_streak


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return float(s[n // 2])
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def compute_personality_scores(behavior_data: dict) -> dict[str, int]:
    """
    计算性格画像各维度的评分 (0-100)
    包含: 坚韧度, 完美主义, 冒险精神, 自律性, 调试耐心, 作息规律

    设计目标：与 LLM 文字评级同向同量级。
    LLM 5/4/3/2/1 星 ≈ 90/70/50/30/15 分。
    """
    scores = {}

    stuck_problems = behavior_data.get("stuck_problems", [])
    total_records = behavior_data.get("total_records", 1)
    total_tried = behavior_data.get("total_unique_problems", 1) or 1
    ac_rate = behavior_data.get("ac_rate", 0) or 0
    first_try_rate = behavior_data.get("first_try_ac_rate", 0) or 0
    ce_rate = behavior_data.get("ce_rate", 0) or 0
    active_rate = behavior_data.get("active_rate", 0) or 0
    max_consecutive = behavior_data.get("max_consecutive_days", 0) or 0
    debug = behavior_data.get("debug_patience", {}) or {}
    quick_rate = debug.get("quick_resubmit_under_60s_rate", 0) or 0
    median_interval = debug.get("median_resubmit_interval_seconds") or 60

    # ---- 1. 坚韧度 (Perseverance) ----
    # 单题死磕强度是核心：30+ 次的卡题 = 顶级坚韧
    stuck_count = len(stuck_problems)
    max_stuck = max((p.get("submit_count", 0) for p in stuck_problems), default=0)
    avg_stuck = (
        sum(p.get("submit_count", 0) for p in stuck_problems) / stuck_count
        if stuck_count else 0
    )
    if max_stuck >= 20:
        base_pers = 70
    elif max_stuck >= 10:
        base_pers = 50
    elif max_stuck >= 5:
        base_pers = 38
    elif stuck_count >= 3:
        base_pers = 32
    else:
        base_pers = 18
    count_bonus = min(20, stuck_count * 4)
    avg_bonus = min(10, avg_stuck * 0.5)
    # AC 率 >= 5% 时按比例加分
    ac_factor = max(0.0, min(15.0, (ac_rate - 0.05) * 50))
    scores["坚韧度"] = int(max(0, min(100, base_pers + count_bonus + avg_bonus + ac_factor)))

    # ---- 2. 完美主义 (Perfectionism) ----
    # 一次 AC 率高 = 写代码细致 + CE 率低 = 语法不马虎 + 不急着重交
    first_try_score = first_try_rate * 55
    ce_penalty = ce_rate * 35
    not_rush_score = (1 - quick_rate) * 20
    scores["完美主义"] = int(max(0, min(100, first_try_score + not_rush_score - ce_penalty)))

    # ---- 3. 冒险精神 (Adventurous Spirit) ----
    # 卡题数量 + 卡题强度（≥5 次的算高强度挑战）
    stuck_count_score = min(40, stuck_count * 8)
    hard_stuck = sum(1 for p in stuck_problems if p.get("submit_count", 0) >= 5)
    hard_score = min(30, hard_stuck * 10)
    base = 25
    scores["冒险精神"] = int(max(0, min(100, stuck_count_score + hard_score + base)))

    # ---- 4. 自律性 (Self-Discipline) ----
    # 时段集中度（top 3 小时占比）+ 峰值集中度 + 星期集中度 + 持续性
    hourly = behavior_data.get("hourly_distribution", {}) or {}
    total_h = sum(hourly.values()) or 1
    sorted_counts = sorted(hourly.values(), reverse=True) if hourly else []
    top1 = sorted_counts[0] if sorted_counts else 0
    top3 = sum(sorted_counts[:3])
    top1_share = top1 / total_h
    top3_share = top3 / total_h

    if top3_share >= 0.5:
        time_score = 55
    elif top3_share >= 0.4:
        time_score = 42
    elif top3_share >= 0.3:
        time_score = 30
    else:
        time_score = 20
    # peak 小时单独大权重（"7:00 整点 106 次"这种信号）
    time_score += min(25, top1_share * 65)
    # 固定训练小时数（每小时达到峰值 1/3 的算"固定时段"）
    threshold = top1 / 3 if top1 else 0
    fixed_hours = sum(1 for v in hourly.values() if v >= threshold and v > 0)
    time_score += min(15, fixed_hours * 2)

    wd = behavior_data.get("weekend_vs_weekday", {}) or {}
    we_total = wd.get("周末", 0) + wd.get("工作日", 0)
    weekend_share = (wd.get("周末", 0) / we_total) if we_total > 0 else 0.5
    # 0=工作日集中, 1=周末集中, 偏离 0.5 越远 = 越有固定训练时段
    week_concentration = abs(weekend_share - 0.5) * 2
    week_score = week_concentration * 20

    habit_score = min(20, max_consecutive * 0.7 + active_rate * 100 * 0.15)

    scores["自律性"] = int(max(0, min(100, time_score + week_score + habit_score)))

    # ---- 5. 调试耐心 (Debugging Patience) ----
    # 主信号：1 分钟内快速重交占比（越低越耐心）
    if quick_rate < 0.10:
        base_dp = 80
    elif quick_rate < 0.20:
        base_dp = 65
    elif quick_rate < 0.30:
        base_dp = 50
    elif quick_rate < 0.40:
        base_dp = 40
    elif quick_rate < 0.50:
        base_dp = 32
    else:
        base_dp = 25
    # 中位数间隔微调
    if median_interval >= 600:
        base_dp += 15
    elif median_interval >= 300:
        base_dp += 10
    elif median_interval >= 120:
        base_dp += 5
    elif median_interval < 60:
        base_dp -= 5
    scores["调试耐心"] = int(max(15, min(100, base_dp)))

    # ---- 6. 作息规律 (Rest Pattern) ----
    # 核心信号：训练时段集中度（top 2 时段占比，越集中 = 越有固定作息）
    time_slots = behavior_data.get("time_slot_distribution", {}) or {}
    total_slots = sum(time_slots.values()) or 1
    sorted_slot_vals = sorted(time_slots.values(), reverse=True)
    top2_slots = sum(sorted_slot_vals[:2])
    top2_share = top2_slots / total_slots

    if top2_share >= 0.95:
        base_rp = 90
    elif top2_share >= 0.85:
        base_rp = 75
    elif top2_share >= 0.70:
        base_rp = 60
    elif top2_share >= 0.50:
        base_rp = 45
    else:
        base_rp = 30

    # 健康时段（早晨/上午/下午）比例微调
    healthy_keys = ["早晨 (6-9点)", "上午 (9-12点)", "下午 (13-17点)"]
    healthy = sum(time_slots.get(k, 0) for k in healthy_keys)
    healthy_share = healthy / total_slots
    if healthy_share >= 0.70:
        health_adj = 10
    elif healthy_share >= 0.40:
        health_adj = 5
    elif healthy_share >= 0.20:
        health_adj = 0
    else:
        health_adj = -5

    scores["作息规律"] = int(max(0, min(100, base_rp + health_adj)))

    return scores

def compute_six_dimension_scores(export_data: dict, behavior_data: dict) -> dict[str, int]:
    """
    计算六维能力评分
    参考 report_public.pdf 中的评分体系
    """
    summary = export_data.get("summary", {}) or {}
    top_tags = summary.get("top_algorithm_tags", []) or summary.get("top_tags", []) or []
    difficulty_histogram = summary.get("difficulty_histogram", {}) or {}
    solved_count = int(export_data.get("solved_count", 0))

    # 计算平均难度
    difficulty_total = 0
    weighted = 0
    for key, value in difficulty_histogram.items():
        if str(key).isdigit():
            difficulty_total += int(value)
            weighted += int(key) * int(value)
    avg_difficulty = weighted / difficulty_total if difficulty_total else 0

    # 标签计数
    tag_counts = {}
    for item in top_tags:
        tag_name = str(item.get("name") or "").lower()
        tag_counts[tag_name] = int(item.get("count", 0))

    def _count_tags(*keywords):
        total = 0
        for tag_name, count in tag_counts.items():
            if any(kw in tag_name for kw in keywords):
                total += count
        return total

    # 基础算法: 枚举/模拟/贪心/递归/二分/排序
    basic_algo = _count_tags("枚举", "模拟", "贪心", "递归", "二分", "排序", "分治", "倍增", "前缀和", "差分")
    # 搜索
    search = _count_tags("搜索", "dfs", "bfs", "回溯", "剪枝", "记忆化")
    # 动态规划
    dp = _count_tags("dp", "动态规划", "背包", "区间", "树形", "状压", "数位", "期望")
    # 图论
    graph = _count_tags("图", "最短路", "并查集", "拓扑", "tarjan", "lca", "树", "网络流", "匹配", "二分图")
    # 数据结构
    ds = _count_tags("线段树", "树状数组", "st表", "单调", "堆", "平衡树", "分块", "莫队", "链表", "栈", "队列")
    # 字符串
    string = _count_tags("字符串", "kmp", "hash", "trie", "sam", "manacher", "ac自动机")
    # 数学
    math_tags = _count_tags("数论", "数学", "组合", "计数", "概率", "期望", "矩阵", "快速幂", "逆元", "欧拉", "gcd", "筛法")

    # 基础算法评分 (参考: 85分需要全面精通)
    score_basic = min(95, 40 + basic_algo * 2 + search * 2 + int(avg_difficulty * 5))

    # 数据结构评分 (参考: 62分)
    score_ds = min(95, 30 + ds * 3 + int(avg_difficulty * 4))

    # 图论评分 (参考: 68分)
    score_graph = min(95, 30 + graph * 3 + int(avg_difficulty * 4))

    # 动态规划评分 (参考: 75分)
    score_dp = min(95, 35 + dp * 3 + int(avg_difficulty * 5))

    # 字符串评分 (参考: 45分)
    score_string = min(95, 25 + string * 4 + int(avg_difficulty * 3))

    # 数学评分 (参考: 40分)
    score_math = min(95, 20 + math_tags * 3 + int(avg_difficulty * 3))

    # 根据AC率和一次AC率微调
    ac_rate = behavior_data.get("ac_rate", 0.5)
    first_try_rate = behavior_data.get("first_try_ac_rate", 0.5)
    adjustment = int((ac_rate + first_try_rate - 1.0) * 10)

    scores = {
        "基础算法": max(20, min(95, score_basic + adjustment)),
        "数据结构": max(20, min(95, score_ds + adjustment)),
        "图论": max(20, min(95, score_graph + adjustment)),
        "动态规划": max(20, min(95, score_dp + adjustment)),
        "字符串": max(20, min(95, score_string + adjustment)),
        "数学": max(20, min(95, score_math + adjustment)),
    }

    return scores


def format_behavior_summary(behavior_data: dict) -> str:
    """将行为分析数据格式化为 Markdown 文本，供 AI prompt 使用"""
    if "error" in behavior_data:
        return f"**提交行为分析**: {behavior_data['error']}"

    lines = []
    lines.append("## 提交行为深度分析")
    lines.append("")
    lines.append(f"- **总提交次数**: {behavior_data.get('total_records', 0)}")
    lines.append(f"- **独立尝试题数**: {behavior_data.get('total_unique_problems', 0)}")
    lines.append(f"- **AC 次数**: {behavior_data.get('ac_count', 0)}")
    lines.append(f"- **整体 AC 率**: {behavior_data.get('ac_rate', 0) * 100:.1f}%")
    lines.append(f"- **一次 AC 率**: {behavior_data.get('first_try_ac_rate', 0) * 100:.1f}%")
    lines.append(f"- **编译错误 (CE) 次数**: {behavior_data.get('ce_count', 0)} ({behavior_data.get('ce_rate', 0) * 100:.1f}%)")
    lines.append(f"- **卡题数（>=3次提交且最终未AC）**: {len(behavior_data.get('stuck_problems', []))}")
    lines.append("")

    lines.append("### 作息规律")
    time_slots = behavior_data.get("time_slot_distribution", {})
    for slot, count in time_slots.items():
        lines.append(f"- {slot}: {count} 次")
    peak = behavior_data.get("peak_hour")
    if peak is not None:
        lines.append(f"- **提交峰值时段**: {peak}:00 ({behavior_data.get('peak_hour_count', 0)} 次)")
    lines.append("")

    lines.append("### 星期分布")
    weekday = behavior_data.get("weekday_distribution", {})
    for day, count in weekday.items():
        lines.append(f"- {day}: {count} 次")
    weekend_vs = behavior_data.get("weekend_vs_weekday", {})
    lines.append(f"- 周末合计: {weekend_vs.get('周末', 0)} 次 | 工作日合计: {weekend_vs.get('工作日', 0)} 次")
    lines.append("")

    lines.append("### 活跃度")
    lines.append(f"- **活跃天数**: {behavior_data.get('active_days', 0)} / {behavior_data.get('total_days_span', 0)} 天")
    lines.append(f"- **活跃率**: {behavior_data.get('active_rate', 0) * 100:.1f}%")
    lines.append(f"- **最大连续训练天数**: {behavior_data.get('max_consecutive_days', 0)} 天")
    max_daily = behavior_data.get('max_daily_submits', 0)
    max_date = behavior_data.get('max_daily_date', '')
    lines.append(f"- **单日最高提交**: {max_daily} 次 ({max_date})")
    lines.append("")

    lines.append("### 调试习惯")
    debug = behavior_data.get("debug_patience", {})
    median_interval = debug.get("median_resubmit_interval_seconds")
    if median_interval is not None:
        lines.append(f"- **WA 后重交间隔中位数**: {median_interval:.0f} 秒 ({median_interval/60:.1f} 分钟)")
    lines.append(f"- **1分钟内快速重交占比**: {debug.get('quick_resubmit_under_60s_rate', 0) * 100:.1f}%")
    lines.append("")

    lines.append("### 死磕题目 TOP")
    stuck = behavior_data.get("stuck_problems", [])
    for i, item in enumerate(stuck[:5], 1):
        lines.append(f"{i}. **{item['pid']}** {item['title']} — {item['submit_count']} 次提交 ({item['final_status']})")
    lines.append("")
    return "\n".join(lines)
