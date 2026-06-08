"""
test_phase3_full.py — Phase 3 端到端测试

v3.5 §7 + §10 验收关键路径:
  - 冲刺营题库种子(28/56 题)
  - 激活码生成/兑换
  - 进度分配 + 标记完成
  - 评估达成(真考 80+ + 完成度 ≥ 90%)
  - 班期通过率统计
  - 跳级成功率统计
  - 营收统计(4 SKU)
  - 政策水印
  - 免初赛里程碑
  - C9 5 校命中
  - 学员 Pro 错题本(StudyMate 链接生成)
"""
import os
import sys
import time
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("TASK_DB_PATH", str(ROOT / "tasks.db"))
os.environ.setdefault("ALLOW_INSECURE_DEFAULT", "1")

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "docs"))

import admin_students
import admin_guardians
import admin_goals
import activation_codes
import camp_curriculum as cc
import phase3_dashboard as dash
from studymate_bridge import build_studymate_url


def banner(s: str) -> None:
    print()
    print("=" * 60)
    print(f"  {s}")
    print("=" * 60)


def section(s: str) -> None:
    print(f"--- {s} ---")


banner("0. 准备测试学员（陈豆豆档案如已存在则保留）")
test_uid = f"phase3_e2e_{int(time.time())}"
existing = admin_students.get_student_by_uid(test_uid)
if existing:
    admin_students.delete_student(existing["id"])
sid = admin_students.create_student(
    test_uid,
    real_name="Phase3 E2E",
    school="测试小学",
    grade="2024",
)
print(f"  [OK] 学员 sid={sid}, uid={test_uid}")


# ==== 1. 冲刺营题库种子 ====
banner("1. 冲刺营题库种子")
n_pop = cc.seed_camp_curriculum("popularize_camp")
n_imp = cc.seed_camp_curriculum("improve_camp")
pop_list = cc.list_camp_problems("popularize_camp")
imp_list = cc.list_camp_problems("improve_camp")
assert len(pop_list) == 28, f"普及组题数异常: {len(pop_list)}"
assert len(imp_list) == 56, f"提高组题数异常: {len(imp_list)}"
print(f"  [OK] popularize={len(pop_list)}, improve={len(imp_list)}")


# ==== 2. 生成 + 兑换冲刺营激活码 ====
banner("2. 生成 + 兑换冲刺营激活码")
code = activation_codes.generate_codes("popularize_camp", count=1, student_id=int(sid))[0]
ac = activation_codes.redeem_code(code, int(sid))
assert ac["student_id"] == int(sid)
print(f"  [OK] code={code}, activation_id={ac['id']}, expires={ac['expires_at']}")


# ==== 3. 分配进度 + 标记完成 ====
banner("3. 分配进度 + 标记前 3 题完成")
n_prog = cc.assign_camp_to_student(int(sid), int(ac["id"]))
assert n_prog == 28
progress = cc.get_camp_progress(int(sid), int(ac["id"]))
for it in progress["items"][:3]:
    cc.mark_problem_done(int(sid), int(it["problem_id"]), 100)
p2 = cc.get_camp_progress(int(sid), int(ac["id"]))
assert p2["submitted"] >= 3
print(f"  [OK] {p2['submitted']}/{p2['total']} ({p2['done_pct']}%)")


# ==== 4. 今日题目 ====
banner("4. 今日题目推送")
today = cc.get_today_problem(int(sid), int(ac["id"]))
assert today and today["day"] >= 1
print(f"  [OK] day {today['day']} = {today['pid']} {today['title'][:30]}")


# ==== 5. 评估达成 ====
banner("5. 评估达成（未真考 = 未达成）")
ev = cc.evaluate_camp_completion(int(sid), int(ac["id"]))
assert not ev["achieved"]
print(f"  [OK] 完成度 {ev['completion_pct']}%, 真考 None, achieved={ev['achieved']}")


# ==== 6. 模拟 GESP 7 级 85 分真考 → 达成 ====
banner("6. GESP 7 级 85 分真考 → 冲刺营达成")
conn = sqlite3.connect(str(ROOT / "tasks.db"))
conn.row_factory = sqlite3.Row
g7 = conn.execute(
    "SELECT id FROM competitions WHERE type='gesp' AND code LIKE '%L7-8%' LIMIT 1"
).fetchone()
# 录入"起步"考试（GESP 1 级），让后续 7 级成为跳级
g1 = conn.execute(
    "SELECT id FROM competitions WHERE type='gesp' AND code LIKE '%L1-4%' "
    "ORDER BY exam_date DESC LIMIT 1"
).fetchone()
conn.close()
# 仅在该学员还没有起步级考试时录入（避免重复；add_gesp_exam 不去重）
existing = admin_students.list_gesp_exams(int(sid))
has_start = any(int(e["registered_level"]) <= 6 for e in existing)
if g1 and not has_start:
    admin_students.add_gesp_exam(int(sid), int(g1["id"]), 1, 60, recorded_by="e2e")
admin_students.add_gesp_exam(int(sid), int(g7["id"]), 7, 85, recorded_by="e2e")
# 完成剩余题目
for it in progress["items"][3:]:
    cc.mark_problem_done(int(sid), int(it["problem_id"]), 100)
ev2 = cc.evaluate_camp_completion(int(sid), int(ac["id"]))
assert ev2["exam_score"] == 85
assert ev2["achieved"] is True
print(f"  [OK] exam=85 + 100% 完成 → achieved={ev2['achieved']}")


# ==== 7. 班期通过率 ====
banner("7. 班期通过率统计")
rate = cc.camp_pass_rate(sku="popularize_camp")
assert rate["achieved"] >= 1
print(f"  [OK] popularize_camp: {rate['achieved']}/{rate['total_enrolled']} = {rate['pass_rate_pct']}%")


# ==== 8. 跳级成功率 ====
banner("8. 跳级成功率统计")
skip = dash.get_skip_success_rate()
assert skip["skip_total"] > 0
print(f"  [OK] 跳级 {skip['skip_total']} 次 / 通过 {skip['skip_passed']} = {skip['skip_pass_rate_pct']}%")


# ==== 8.5 额外再生成 1 个不同 SKU 的兑换码（保证 9 步 n_skus >= 2）====
# 给同测试学员多开一个提高组冲刺（improve_camp）激活码并兑换，提升 SKU 种类数
extra_code = activation_codes.generate_codes("improve_camp", count=1, student_id=int(sid))[0]
activation_codes.redeem_code(extra_code, int(sid))
print(f"  [OK] 额外 SKU 兑换: improve_camp code={extra_code}")


# ==== 9. 营收统计 ====
banner("9. 营收统计（4 SKU）")
rev = dash.get_revenue_stats(days=365)
assert rev["total_revenue_cny"] >= 99  # 至少 ¥99（我们的 1 单）
n_skus = len(rev["by_sku"])
assert n_skus >= 2
print(f"  [OK] 总营收 ¥{rev['total_revenue_cny']}, {n_skus} 个 SKU 已兑换")


# ==== 10. 政策水印 ====
banner("10. 政策日历数据过期水印")
n_ev = cc.seed_policy_events(force=False)
last_updated = cc.get_policy_events_last_updated()
assert last_updated is not None
print(f"  [OK] 政策事件 {n_ev} 条, 水印 {last_updated}")


# ==== 11. 免初赛里程碑 ====
banner("11. 免初赛里程碑")
em = dash.get_exemption_milestone_stats()
print(f"  [OK] CSP-J 免 {em['csp_j_exempt_students']} + CSP-S 免 {em['csp_s_exempt_students']} = {em['total_exempt_students']} (目标 ≥ {em['milestone_target']})")


# ==== 12. C9 强基 5 校命中 ====
banner("12. C9 强基 5 校命中（§8 反向 Scope: 只做 5 所）")
admin_goals.upsert_student_goal(int(sid), primary_path="强基", target_university="清华大学", target_province="北京")
c9 = dash.get_c9_quota_status()
assert "清华大学" in c9["sample_universities"]
assert len(c9["sample_universities"]) == 5
print(f"  [OK] 5 所样板: {c9['sample_universities']}")
print(f"        命中: {c9['student_counts']}")


# ==== 13. 学员 Pro 错题本 ====
banner("13. 学员 Pro 错题本 + StudyMate 链接")
# 这个学员是新档案，没有错题 → 测 UID 999101 (UID-999101 = seed_demo 中的 e2e_demo)
url = build_studymate_url("P1000", student_id=int(sid), gesp_level=5)
assert "pid=P1000" in url
assert "student_id=" in url
assert "gesp_level=5" in url
assert "token=" in url
print(f"  [OK] StudyMate URL 含: pid / student_id / gesp_level / token")
print(f"        示例: {url[:90]}...")


# ==== 14. 完整 dashboard ====
banner("14. 完整 dashboard 聚合")
d = dash.get_full_dashboard()
assert d["revenue"]["total_revenue_cny"] >= 0
assert d["policy_data_last_updated"] is not None
assert "skip_pass_rate_pct" in d["skip_success"]
print(f"  [OK] dashboard 区块: {list(d.keys())}")


# ==== 清理 ====
banner("15. 清理")
admin_students.delete_student(int(sid))
print(f"  [OK] 清理测试学员 id={sid}")


print()
print("=" * 60)
print("[OK] Phase 3 端到端测试全部通过（14 项）")
print("=" * 60)
