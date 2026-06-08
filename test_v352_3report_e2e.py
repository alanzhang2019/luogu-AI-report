"""v3.5.2 3 版本报告端到端测试"""
import time
import sys
sys.path.insert(0, r"d:\AItrade\luoguAI\luogu-AI-report")

from web_app import app
import admin_students
import admin_guardians

client = app.test_client()
ok = 0
total = 0

def banner(s):
    print(f"\n=== {s} ===")

# 1. 首页 CTA 重排：必须有"AI 生成学习报告"最大按钮
banner("1. 首页 CTA 重排：'AI 生成学习报告' 最大按钮")
r = client.get("/")
total += 1
if r.status_code == 200:
    body = r.get_data(as_text=True)
    checks = {
        "v3.5.2 主入口 chip": "v3.5.2 主入口" in body,
        "AI 生成学习报告标题": "AI 生成学习报告" in body,
        "主 CTA 按钮": "立即生成我的学习报告" in body,
        "3 身份入口保留": "我是选手" in body and "我是家长" in body and "我是教练" in body,
        "UID 快速入口保留": "meUid" in body,
    }
    all_ok = all(checks.values())
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    if all_ok: ok += 1
    print(f"  📦 {len(body)} bytes")
else:
    print(f"  ❌ HTTP {r.status_code}")

# 2. /select-mode 引导页
banner("2. /select-mode 引导页（GET + POST）")
r = client.get("/select-mode")
total += 1
if r.status_code == 200:
    body = r.get_data(as_text=True)
    if "洛谷 UID" in body and "立即生成我的学习报告" in body:
        print("  ✅ GET /select-mode 渲染正常")
        ok += 1
    else:
        print(f"  ❌ 内容缺失")

# POST 无效 UID
r = client.post("/select-mode", data={"luogu_uid": "abc"})
total += 1
if r.status_code == 400:
    print("  ✅ POST 无效 UID 400 错误处理")
    ok += 1
else:
    print(f"  ❌ HTTP {r.status_code}")

# POST 有效但未注册 UID
r = client.post("/select-mode", data={"luogu_uid": "99999999"}, follow_redirects=False)
total += 1
if r.status_code == 302 and "/register" in r.headers.get("Location", ""):
    print("  ✅ POST 未注册 UID → 302 /register")
    ok += 1
else:
    print(f"  ❌ HTTP {r.status_code}, Location: {r.headers.get('Location', 'none')}")

# POST 已注册 UID
ts = int(time.time() * 1000) % 1000000000
test_uid = f"2030{ts:05d}"[-9:].zfill(9)
r = client.post("/register", data={
    "city": "杭州", "real_name": "测试3版", "grade": "JUNIOR_2", "gender": "M",
    "luogu_uid": test_uid, "phone": "13900139999", "agree": "on",
}, follow_redirects=False)
r = client.post("/select-mode", data={"luogu_uid": test_uid}, follow_redirects=False)
total += 1
if r.status_code == 302 and f"/me/{test_uid}" in r.headers.get("Location", ""):
    print("  ✅ POST 已注册 UID → 302 /me/<uid>")
    ok += 1
else:
    print(f"  ❌ HTTP {r.status_code}, Location: {r.headers.get('Location', 'none')}")

# 3. /report/student/<uid> 学员版报告（游戏化）
banner("3. 学员版报告 /report/student/<uid>")
r = client.get(f"/report/student/{test_uid}")
total += 1
if r.status_code == 200:
    body = r.get_data(as_text=True)
    checks = {
        "学员版报告标题": "学员版报告" in body,
        "Hi 称呼": "Hi，" in body and ("测试3版" in body or "选手" in body),
        "段位大徽章（无GESP=🌱）": "🌱" in body or "GESP 0" in body,
        "进度条": "progress-fill" in body,
        "错题本卡片": "错题本" in body,
        "AI 讲题锁定（无家长订阅）": "需家长订阅" in body or "🔒" in body,
        "加 V 兑换码提示": "PS-XXXXXXXX" in body,
        "下一步行动（游戏化）": "下一步行动" in body,
        "Tab 切换 学员版/家长版": "学员版（当前）" in body,
    }
    all_ok = all(checks.values())
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    if all_ok: ok += 1
    print(f"  📦 {len(body)} bytes")
else:
    print(f"  ❌ HTTP {r.status_code}")

# 4. /report/parent/<token> 家长版报告（决策树 + 政策匹配）
banner("4. 家长版报告 /report/parent/<token>")
stu = admin_students.get_student_by_uid(test_uid)
g = admin_guardians.create_guardian(
    student_id=stu['id'],
    phone="13900139999",
    email=None,
    display_name="3版测试家长",
    notify_channel="wechat",
)
g_token = g["notify_token"] if isinstance(g, dict) else g
r = client.get(f"/report/parent/{g_token}")
total += 1
if r.status_code == 200:
    body = r.get_data(as_text=True)
    checks = {
        "家长版报告标题": "家长版报告" in body,
        "您家孩子称呼": "您家孩子" in body,
        "城市/年级/段位/错题 4 卡": "城市" in body and "年级" in body and "段位" in body and "错题" in body,
        "决策树 3 方案": "方案 A" in body and "方案 B" in body and "方案 C" in body,
        "政策匹配摘要": "升学路径匹配" in body,
        "政策匹配数据：杭州自招高中": "杭州" in body and "自招" in body,
        "周报与赛事日历入口": "周报与赛事日历" in body,
        "Tab 切换 家长版（当前）": "家长版（当前）" in body,
    }
    all_ok = all(checks.values())
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    if all_ok: ok += 1
    print(f"  📦 {len(body)} bytes")
else:
    print(f"  ❌ HTTP {r.status_code}")

# 5. /report/coach 教练版报告（admin 复用）
banner("5. 教练版报告 /report/coach（需 admin 登录）")
# 先登录（默认凭据）
sess = app.test_client()
for u, p in [("admin", "KCJ@6666"), ("admin", "admin"), ("admin", "demo123")]:
    r = sess.post("/admin/login", data={"username": u, "password": p}, follow_redirects=False)
    if r.status_code == 302:
        print(f"  admin 登录成功 ({u})")
        break
# 5 大指标看板
r = sess.get("/report/coach")
total += 1
if r.status_code == 200:
    body = r.get_data(as_text=True)
    checks = {
        "教练版报告标题": "教练版报告" in body,
        "班级看板": "班级看板" in body,
        "5 大指标看板": "班级学员数" in body and "通过 GESP" in body and "免 CSP-J" in body and "免 CSP-S" in body and "本期营收" in body,
        "Top 20 学员表": "Top 20 学员" in body,
        "操作入口 4 个": "学员管理" in body and "营收看板" in body and "兑换码生成" in body and "新增学员" in body,
    }
    all_ok = all(checks.values())
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    if all_ok: ok += 1
    print(f"  📦 {len(body)} bytes")
else:
    print(f"  ❌ HTTP {r.status_code}")

# 6. 错误路径
banner("6. 错误路径处理")
r = client.get("/report/student/00000000")  # 不存在
total += 1
if r.status_code == 404:
    print("  ✅ 学员不存在 → 404")
    ok += 1
else:
    print(f"  ❌ HTTP {r.status_code}")

r = client.get("/report/parent/invalid_token")
total += 1
if r.status_code == 404:
    print("  ✅ 家长 token 无效 → 404")
    ok += 1
else:
    print(f"  ❌ HTTP {r.status_code}")

print(f"\n{'='*60}")
print(f"v3.5.2 3 版本报告端到端: {ok}/{total} 通过")
print(f"{'='*60}")
sys.exit(0 if ok == total else 1)
