"""v3.5.2 政策匹配端到端：注册不同学段学员 → 家长端展示匹配"""
import re
import sys
import time
sys.path.insert(0, r"d:\AItrade\luoguAI\luogu-AI-report")

from web_app import app
import admin_students
import admin_guardians

client = app.test_client()
ok = 0
total = 0

def banner(s):
    print(f"\n=== {s} ===")

# 准备：注册一个杭州 JUNIOR_2 的学员
banner("0a. 准备：注册一个 杭州 JUNIOR_2 学员")
ts = int(time.time() * 1000) % 1000000000
hz_uid = "202600" + str(ts)[-3:].zfill(3)
r = client.post("/register", data={
    "city": "杭州", "real_name": "杭州初二生", "grade": "JUNIOR_2", "gender": "M",
    "luogu_uid": hz_uid, "birth_date": "2010-05-01", "phone": "13900139000", "agree": "on",
}, follow_redirects=False)
total += 1
if r.status_code == 302:
    print(f"  ✅ 杭州初二生注册成功 UID {hz_uid}")
    ok += 1
else:
    print(f"  ❌ 注册失败 {r.status_code}: {r.get_data(as_text=True)[:200]}")

# 准备：注册一个 北京 PRIMARY_3 的学员
banner("0b. 准备：注册一个 北京 PRIMARY_3 学员")
ts = int(time.time() * 1000) % 1000000000
bj_uid = "202700" + str(ts)[-3:].zfill(3)
r = client.post("/register", data={
    "city": "北京", "real_name": "北京小学生", "grade": "PRIMARY_3", "gender": "F",
    "luogu_uid": bj_uid, "birth_date": "2015-09-01", "phone": "13900139001", "agree": "on",
}, follow_redirects=False)
total += 1
if r.status_code == 302:
    print(f"  ✅ 北京小学生注册成功 UID {bj_uid}")
    ok += 1
else:
    print(f"  ❌ 注册失败 {r.status_code}")

# 准备：注册一个 SENIOR_1 学员（验证强基匹配）
banner("0c. 准备：注册一个 SENIOR_1 学员（杭州高一）")
ts = int(time.time() * 1000) % 1000000000
sn_uid = "202800" + str(ts)[-3:].zfill(3)
r = client.post("/register", data={
    "city": "杭州", "real_name": "杭州高一", "grade": "SENIOR_1", "gender": "M",
    "luogu_uid": sn_uid, "birth_date": "2008-09-01", "phone": "13900139002", "agree": "on",
}, follow_redirects=False)
total += 1
if r.status_code == 302:
    print(f"  ✅ 杭州高一注册成功 UID {sn_uid}")
    ok += 1
else:
    print(f"  ❌ 注册失败 {r.status_code}")

# 1. /parent/<token> 杭州初二 → 匹配自招高中
banner("1. 杭州初二生 → 家长端匹配 自招高中")
hz_stu = admin_students.get_student_by_uid(hz_uid)
print(f"  学员: {hz_stu['real_name']} 城市 {hz_stu['city']} 年级 {hz_stu['grade']}")

# 创建家长 token
hz_token = admin_guardians.create_guardian(
    student_id=hz_stu['id'],
    phone="13900139000",
    email=None,
    display_name="杭州初二生家长",
    notify_channel="wechat",
)
hz_token_str = hz_token["notify_token"] if isinstance(hz_token, dict) else hz_token
print(f"  家长 token: {hz_token_str}")

r = client.get(f"/parent/{hz_token_str}")
total += 1
if r.status_code == 200:
    body = r.get_data(as_text=True)
    checks = {
        "标题'升学路径匹配'": "升学路径匹配" in body,
        "v3.5.2 chip": "v3.5.2" in body,
        "学段显示'初中（初二）'": "初中（初二）" in body,
        "城市显示'杭州'": "杭州" in body,
        "匹配类型'自招高中'": "自招高中" in body,
        "杭州第二中学（滨江校区）": "杭州第二中学（滨江校区）" in body,
        "学军中学": "学军中学" in body,
        "招 80 人数据": "招生 80 人" in body,
        "推荐徽章 ⭐": "⭐ 推荐" in body,
        "查看政策链接": "查看政策" in body,
    }
    all_ok = all(checks.values())
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    if all_ok:
        ok += 1
    print(f"  📦 {len(body)} bytes")
else:
    print(f"  ❌ HTTP {r.status_code}")

# 2. /parent/<token> 北京小学 → 匹配科技特长生中学
banner("2. 北京小学三年级 → 家长端匹配 科技特长生中学")
bj_stu = admin_students.get_student_by_uid(bj_uid)
bj_token_raw = admin_guardians.create_guardian(
    student_id=bj_stu['id'],
    phone="13900139001",
    email=None,
    display_name="北京小学生家长",
    notify_channel="wechat",
)
bj_token = bj_token_raw["notify_token"] if isinstance(bj_token_raw, dict) else bj_token_raw
r = client.get(f"/parent/{bj_token}")
total += 1
if r.status_code == 200:
    body = r.get_data(as_text=True)
    checks = {
        "学段显示'小学（3 年级）'": "小学（3 年级）" in body,
        "匹配类型'科技特长生中学'": "科技特长生中学" in body,
        "人大附中早培班": "人大附中早培班" in body,
        "CSP-J 一等奖免初试": "CSP-J 一等奖" in body or "CSP-J 一等" in body,
        "+ 30 分": "+ 30 分" in body,
    }
    all_ok = all(checks.values())
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    if all_ok:
        ok += 1
    print(f"  📦 {len(body)} bytes")
else:
    print(f"  ❌ HTTP {r.status_code}")

# 3. /parent/<token> 杭州高一 → 强基 5 校
banner("3. 杭州高一 → 家长端匹配 强基 5 校")
sn_stu = admin_students.get_student_by_uid(sn_uid)
sn_token_raw = admin_guardians.create_guardian(
    student_id=sn_stu['id'],
    phone="13900139002",
    email=None,
    display_name="杭州高一家长",
    notify_channel="wechat",
)
sn_token = sn_token_raw["notify_token"] if isinstance(sn_token_raw, dict) else sn_token_raw
r = client.get(f"/parent/{sn_token}")
total += 1
if r.status_code == 200:
    body = r.get_data(as_text=True)
    n_qj = body.count("强基计划")
    checks = {
        "学段显示'高中（高一）'": "高中（高一）" in body,
        "匹配类型'强基大学'": "强基大学" in body,
        "清华大学": "清华大学" in body,
        "北京大学": "北京大学" in body,
        "复旦大学": "复旦大学" in body,
        "上海交通大学": "上海交通大学" in body,
        "浙江大学": "浙江大学" in body,
        "5 所强基": n_qj >= 5,
    }
    all_ok = all(checks.values())
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    if all_ok:
        ok += 1
    print(f"  📦 {len(body)} bytes · 强基出现 {n_qj} 次")
else:
    print(f"  ❌ HTTP {r.status_code}")

# 4. /parent/<token> 未填写城市 → 降级
banner("4. 未填写城市 → 降级提示")
ts = int(time.time() * 1000) % 1000000000
em_uid = "202900" + str(ts)[-3:].zfill(3)
# 通过 admin 绕过 city 必填（直接 SQL）
from task_store import _get_conn
conn = _get_conn()
try:
    conn.execute(
        "INSERT INTO students (luogu_uid, real_name, grade, city) VALUES (?, ?, ?, '')",
        (em_uid, "无城市学员", "PRIMARY_1",),
    )
    conn.commit()
finally:
    conn.close()
em_stu = admin_students.get_student_by_uid(em_uid)
em_token_raw = admin_guardians.create_guardian(
    student_id=em_stu['id'],
    phone="13900139003",
    email=None,
    display_name="无城市家长",
    notify_channel="wechat",
)
em_token = em_token_raw["notify_token"] if isinstance(em_token_raw, dict) else em_token_raw
r = client.get(f"/parent/{em_token}")
total += 1
if r.status_code == 200:
    body = r.get_data(as_text=True)
    if "暂无可匹配升学路径" in body or "城市未填" in body or "无匹配数据" in body:
        print(f"  ✅ 降级提示正确")
        ok += 1
    else:
        print(f"  ❌ 未显示降级提示")
else:
    print(f"  ❌ HTTP {r.status_code}")

print(f"\n{'='*50}")
print(f"v3.5.2 政策匹配端到端: {ok}/{total} 通过")
print(f"{'='*50}")
sys.exit(0 if ok == total else 1)
