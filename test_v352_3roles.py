"""v3.5.2 终态验证：3 身份 + AI 讲题受家长订阅控制"""
import re
import sys
sys.path.insert(0, r"d:\AItrade\luoguAI\luogu-AI-report")

import time
from web_app import app
import admin_students
import task_store

def banner(s):
    print(f"\n=== {s} ===")

# 重置测试：找一个 demo 学员，绑定家长订阅，看 /me 是否显示已激活
client = app.test_client()
ok_count = 0
total = 0

# 0. 准备：先注册一个测试学员
banner("0. 准备：注册一个测试学员")
ts = int(time.time() * 1000) % 1000000000
test_uid = str(ts)[-9:].zfill(9)
r = client.post("/register", data={
    "city": "北京", "real_name": "v352测试", "grade": "2025", "gender": "M",
    "luogu_uid": test_uid, "birth_date": "2012-01-01", "phone": "13800138000", "agree": "on",
}, follow_redirects=False)
if r.status_code == 302:
    print(f"  ✅ 注册成功 · UID {test_uid}")
    ok_count += 1
else:
    print(f"  ❌ 注册失败 {r.status_code}")
total += 1

# 1. 首页：3 身份入口（无 AI 讲题独立卡片）
banner("1. GET / 首页 3 身份")
r = client.get("/")
total += 1
if r.status_code == 200:
    body = r.get_data(as_text=True)
    checks = {
        "🎓 我是选手": "我是选手" in body,
        "👨‍👩‍👧 我是家长": "我是家长" in body,
        "🎯 我是教练": "我是教练" in body,
        "3 个 role-card 卡片 (class='role-card ')": body.count('class="role-card ') == 3,
        "AI 讲题不在 role-card 卡片标题中": not any(
            "AI 讲题" in m.group(0)
            for m in re.finditer(r'<a href="[^"]+" class="role-card[^"]*"[^>]*>.*?</a>', body, re.DOTALL)
        ),
        "保留加 V 通道": "xinjing-ai-vip" in body,
        "基本功能免费": "基本功能免费" in body,
    }
    all_ok = all(checks.values())
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    if all_ok:
        ok_count += 1
else:
    print(f"  ❌ HTTP {r.status_code}")

# 2. /me/<uid> 无家长订阅：AI 讲题锁定
banner("2. GET /me/<uid> 无家长订阅 → AI 讲题锁定")
r = client.get(f"/me/{test_uid}")
total += 1
if r.status_code == 200:
    body = r.get_data(as_text=True)
    checks = {
        "选手面板渲染": "UID" in body,
        "AI 讲题需家长订阅（无订阅）": "需家长" in body and "AI 讲题" in body,
        "兑换家长订阅码按钮": "兑换家长订阅码" in body,
        "PARENT-SUB 提示": "PARENT-SUB" in body,
        "未显示'已激活'": "已激活" not in body,
    }
    all_ok = all(checks.values())
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    if all_ok:
        ok_count += 1
    print(f"  📦 {len(body)} bytes")
else:
    print(f"  ❌ HTTP {r.status_code}")

# 3. /redeem 兑换码页：3 SKU 卡片
banner("3. GET /redeem 3 SKU 卡片（无 STUDENT-PRO）")
r = client.get("/redeem")
total += 1
if r.status_code == 200:
    body = r.get_data(as_text=True)
    # 计算 class="sku-card xxx" 实例（不是 CSS 中的 .sku-card）
    sku_cards = len(re.findall(r'class="sku-card\s', body))
    checks = {
        "PS-XXXXXXXX 家长订阅码": "PS-XXXXXXXX" in body,
        "PJC-XXXXXXXX 普及冲刺码": "PJC-XXXXXXXX" in body,
        "IC-XXXXXXXX 提高冲刺码": "IC-XXXXXXXX" in body,
        "STUDENT-PRO 已删除": "STUDENT-PRO" not in body,
        "AI 讲题已含在家长订阅内": "AI 讲题已含在" in body,
        "+ 解锁选手 AI 讲题": "+ 解锁选手 AI 讲题" in body,
        "3 个 SKU 卡片 (不是 4)": sku_cards == 3,
    }
    all_ok = all(checks.values())
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    if all_ok:
        ok_count += 1
else:
    print(f"  ❌ HTTP {r.status_code}")

# 4. 模拟：插入家长订阅码 + 激活 → 再访问 /me 应该看到已激活
banner("4. 激活家长订阅 → AI 讲题可用")
try:
    from task_store import _get_conn
    # 查找一个未使用的 parent_sub 码
    conn = _get_conn()
    try:
        # 优先复用现有未使用的码
        row = conn.execute(
            "SELECT * FROM activation_codes WHERE sku='parent_sub' AND redeemed_at IS NULL LIMIT 1"
        ).fetchone()
        if row:
            code = row["code"]
        else:
            # 创建一个新码
            import secrets
            code = "PS-TEST" + secrets.token_hex(4).upper()
            conn.execute(
                "INSERT INTO activation_codes (code, sku, duration_days, created_by) VALUES (?, 'parent_sub', 30, 'test')",
                (code,),
            )
            conn.commit()
    finally:
        conn.close()

    # 用 /redeem POST 激活
    r = client.post("/redeem", data={"code": code, "student_uid": test_uid}, follow_redirects=False)
    if r.status_code == 200:
        body = r.get_data(as_text=True)
        if "激活成功" in body and test_uid in body:
            print(f"  ✅ /redeem 激活成功 · 码 {code}")
            # 再次访问 /me
            r = client.get(f"/me/{test_uid}")
            body = r.get_data(as_text=True)
            if "已激活" in body and "AI 讲题可用" in body:
                print(f"  ✅ /me 显示'家长订阅已激活 · AI 讲题可用'")
                ok_count += 1
            else:
                print(f"  ❌ /me 未显示已激活")
        else:
            print(f"  ❌ /redeem 激活失败: {body[:200]}")
    else:
        print(f"  ❌ /redeem HTTP {r.status_code}")
    total += 1
except Exception as e:
    print(f"  ❌ 错误: {e}")
    total += 1

# 5. /me/<unknown> 未注册
banner("5. GET /me/999999999 未注册")
r = client.get("/me/999999999")
total += 1
if r.status_code == 404:
    print(f"  ✅ 404 正常")
    ok_count += 1
else:
    print(f"  ❌ HTTP {r.status_code}")

print(f"\n{'='*40}")
print(f"v3.5.2 3 身份 + AI 讲题受家长订阅控制: {ok_count}/{total} 通过")
print(f"{'='*40}")
sys.exit(0 if ok_count == total else 1)
