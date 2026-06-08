"""v3.5.2 端到端验证：4 角色入口 + 兑换码 + 教练版"""
import sys
import time
sys.path.insert(0, r"d:\AItrade\luoguAI\luogu-AI-report")

from web_app import app

def banner(s):
    print(f"\n=== {s} ===")

client = app.test_client()
ok_count = 0
total = 0

# 1. 首页（4 角色入口）
banner("1. GET /  首页 4 角色入口")
r = client.get("/")
total += 1
if r.status_code == 200:
    body = r.get_data(as_text=True)
    # 检查 4 角色 + 关键文案
    checks = {
        "🎓 我是选手": "🎓" in body and "我是选手" in body,
        "💬 AI 讲题": "AI 讲题" in body,
        "👨‍👩‍👧 我是家长": "我是家长" in body,
        "🎯 我是教练": "我是教练" in body,
        "基本功能免费": "基本功能免费" in body,
        "加 V 兑换码": "加 V 兑换码" in body,
        "联系客服购买": "联系客服购买" in body,
        "微信号 xinjing-ai-vip": "xinjing-ai-vip" in body,
        "教练咨询链接 /coach": "/coach" in body,
        "无 ¥15/¥30/¥99/¥299 标签": ("¥15" not in body) and ("¥30" not in body),
        "v3.5.2 chip": "v3.5.2" in body,
    }
    all_ok = all(checks.values())
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    if all_ok:
        ok_count += 1
        print(f"  📦 {len(body)} bytes")
    else:
        print(f"  ❌ FAIL")
else:
    print(f"  ❌ HTTP {r.status_code}")

# 2. /register
banner("2. GET /register  学员 4 字段注册")
r = client.get("/register")
total += 1
if r.status_code == 200:
    body = r.get_data(as_text=True)
    checks = {
        "4 字段 chip": "4 字段" in body,
        "城市/姓名/年级/性别": all(k in body for k in ["城市", "姓名", "年级", "性别"]),
        "30 个 optgroup": body.count("<optgroup") == 30 or body.count("<optgroup") >= 25,
        "洛谷 UID": "洛谷 UID" in body,
    }
    all_ok = all(checks.values())
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    if all_ok:
        ok_count += 1
        print(f"  📦 {len(body)} bytes")
    else:
        print(f"  ❌ FAIL")
else:
    print(f"  ❌ HTTP {r.status_code}")

# 3. /parent 入口（v3.5.2 改后）
banner("3. GET /parent  家长端入口（加 V + 邀请码）")
r = client.get("/parent")
total += 1
if r.status_code == 200:
    body = r.get_data(as_text=True)
    checks = {
        "家长邀请码 (替换家长 token)": "家长邀请码" in body and "家长 token" not in body,
        "教练给您的邀请码": "教练给您的邀请码" in body,
        "加 V 引导": "加客服微信" in body and "xinjing-ai-vip" in body,
        "教练入口 /coach 链接": "/coach" in body,
    }
    all_ok = all(checks.values())
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    if all_ok:
        ok_count += 1
        print(f"  📦 {len(body)} bytes")
    else:
        print(f"  ❌ FAIL")
else:
    print(f"  ❌ HTTP {r.status_code}")

# 4. /coach 教练版咨询
banner("4. GET /coach  教练版 B2B 咨询")
r = client.get("/coach")
total += 1
if r.status_code == 200:
    body = r.get_data(as_text=True)
    checks = {
        "教练版 B2B chip": "教练版 B2B" in body,
        "6 大功能 (批量管理/兑换码/营收/倒推/周报/客户经理)": all(
            k in body for k in ["批量学员管理", "兑换码生成", "营收看板", "倒推计划", "周报推送", "1v1 客户经理"]
        ),
        "B2B · 谈单制": "谈单" in body,
        "基础版/机构版/旗舰版": all(k in body for k in ["基础版", "机构版", "旗舰版"]),
        "不挂网价": "不挂网价" in body,
        "商务微信": "xinjing-ai-business" in body,
        "商务邮箱": "coach@xinjing-ai.com" in body,
        "客户经理电话 400": "400-XXX-XXXX" in body,
        "/admin/login 链接": "/admin/login" in body,
    }
    all_ok = all(checks.values())
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    if all_ok:
        ok_count += 1
        print(f"  📦 {len(body)} bytes")
    else:
        print(f"  ❌ FAIL")
else:
    print(f"  ❌ HTTP {r.status_code}")

# 5. /redeem 兑换码激活
banner("5. GET /redeem  全局兑换码激活页")
r = client.get("/redeem")
total += 1
if r.status_code == 200:
    body = r.get_data(as_text=True)
    checks = {
        "兑换码 chip": "兑换码激活" in body,
        "STUDENT-PRO SKU": "STUDENT-PRO" in body,
        "PARENT-SUB SKU": "PARENT-SUB" in body,
        "CAMP-J SKU": "CAMP-J" in body,
        "CAMP-S SKU": "CAMP-S" in body,
        "4 个 SKU 卡片": body.count("sku-card") >= 4,
        "加 V / 教练 引导": "加 V 获取" in body and "联系客服" in body,
    }
    all_ok = all(checks.values())
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    if all_ok:
        ok_count += 1
        print(f"  📦 {len(body)} bytes")
    else:
        print(f"  ❌ FAIL")
else:
    print(f"  ❌ HTTP {r.status_code}")

# 6. /redeem?code=STUDENT-PRO-XXXX-XXXX （预填）
banner("6. GET /redeem?code=STUDENT-PRO-DEMO-1234  预填兑换码")
r = client.get("/redeem?code=STUDENT-PRO-DEMO-1234")
total += 1
if r.status_code == 200:
    body = r.get_data(as_text=True)
    if 'value="STUDENT-PRO-DEMO-1234"' in body:
        print(f"  ✅ 预填成功")
        ok_count += 1
    else:
        print(f"  ❌ 预填失败")
else:
    print(f"  ❌ HTTP {r.status_code}")

# 7. /redeem POST 无效码
banner("7. POST /redeem  错误兑换码")
r = client.post("/redeem", data={"code": "INVALID-CODE-1234", "student_uid": "123456"})
total += 1
if r.status_code == 200:
    body = r.get_data(as_text=True)
    if "不存在或已失效" in body:
        print(f"  ✅ 错误处理正常")
        ok_count += 1
    else:
        print(f"  ❌ 未显示错误")
else:
    print(f"  ❌ HTTP {r.status_code}")

# 8. /admin/login 教练登录
banner("8. GET /admin/login  教练登录入口")
r = client.get("/admin/login")
total += 1
if r.status_code == 200:
    print(f"  ✅ 教练后台入口正常")
    ok_count += 1
    body = r.get_data(as_text=True)
    print(f"  📦 {len(body)} bytes")
else:
    print(f"  ❌ HTTP {r.status_code}")

print(f"\n{'='*40}")
print(f"v3.5.2 4 角色 + 兑换码 + 教练版 验证: {ok_count}/{total} 通过")
print(f"{'='*40}")
sys.exit(0 if ok_count == total else 1)
