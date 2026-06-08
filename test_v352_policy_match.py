"""v3.5.2 政策匹配引擎验证"""
import sys
sys.path.insert(0, r"d:\AItrade\luoguAI\luogu-AI-report")
import task_store

print("=" * 60)
print("v3.5.2 政策匹配引擎（家长版核心）端到端验证")
print("=" * 60)

ok = 0
total = 0

# 1. 种子数据完整性
print("\n[1] 种子数据完整性")
for school_type, label in [
    ("tech_talent_junior", "科技特长生中学"),
    ("self_enroll_senior", "自招高中"),
    ("qiangji_university", "强基大学"),
]:
    n = task_store._get_conn().execute(
        "SELECT COUNT(*) FROM policy_match_schools WHERE school_type=?", (school_type,)
    ).fetchone()[0]
    print(f"  ✅ {label}: {n} 所")

# 2. 城市 × 学段组合
print("\n[2] 6 城市 × 学段匹配测试")
cities = ["北京", "上海", "杭州", "深圳", "成都", "南京"]
stages = [
    ("PRIMARY_3", "primary", "tech_talent_junior"),
    ("JUNIOR_2", "junior", "self_enroll_senior"),
]
for city in cities:
    for grade, expected_stage, expected_type in stages:
        student = {"city": city, "grade": grade}
        r = task_store.match_school_for_student(student)
        actual_stage = r["stage"]
        actual_type = r["match_type"]
        n_matches = len(r["matches"])
        status = "✅" if (actual_stage == expected_stage and actual_type == expected_type and n_matches > 0) else "❌"
        if status == "✅":
            ok += 1
        total += 1
        print(f"  {status} {city} {grade}  → {actual_stage}({r['stage_label']})  type={r['match_type_label']}  匹配={n_matches} 所")
        if n_matches > 0:
            top = r["matches"][0]
            print(f"      首推: {top['school_name']} | {top['policy_summary']} | 招 {top['enrollment_count']}")

# 3. 高中 → 强基 5 校（不区分城市）
print("\n[3] 高中 → 强基 5 校（全国统一）")
for city in ["北京", "杭州", "深圳", "广州", "成都"]:
    student = {"city": city, "grade": "SENIOR_1"}
    r = task_store.match_school_for_student(student)
    n = len(r["matches"])
    status = "✅" if (r["stage"] == "senior" and r["match_type"] == "qiangji_university" and n == 5) else "❌"
    if status == "✅":
        ok += 1
    total += 1
    print(f"  {status} {city} SENIOR_1  → {n} 所强基大学")
    for m in r["matches"]:
        print(f"      · {m['school_name']} | {m['policy_summary']}")

# 4. 大学/未识别学段
print("\n[4] 大学/已毕业/未识别学段降级")
for grade, expected_stage, expected_type in [
    ("SENIOR_3", "senior", "qiangji_university"),
    ("UNIV_1", "college", None),
    ("GRADUATED", "graduated", None),
    ("未知年级", "unknown", None),
]:
    student = {"city": "北京", "grade": grade}
    r = task_store.match_school_for_student(student)
    status = "✅" if r["stage"] == expected_stage and r["match_type"] == expected_type else "❌"
    if status == "✅":
        ok += 1
    total += 1
    print(f"  {status} grade='{grade}'  → stage={r['stage']}({r['stage_label']})  type={r['match_type_label'] or '(无)'}")

# 5. 城市 → 省份降级
print("\n[5] 城市 → 省份降级（未直辖市的城市）")
student = {"city": "宁波", "grade": "JUNIOR_2"}  # 宁波 → 浙江
r = task_store.match_school_for_student(student)
# 应该匹配浙江省的学校
print(f"  城市：{r['city']}  省份：{r['province']}  匹配 {len(r['matches'])} 所")
if r["province"] == "浙江" and len(r["matches"]) > 0:
    print(f"  ✅ 省份降级正确")
    ok += 1
else:
    print(f"  ❌ 省份降级失败")
total += 1

print(f"\n{'='*60}")
print(f"政策匹配引擎: {ok}/{total} 通过")
print(f"{'='*60}")
sys.exit(0 if ok == total else 1)
