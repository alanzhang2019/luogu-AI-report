"""
test_admin_students_routes.py — 端到端测试学员档案 admin 路由

用 Flask test_client 模拟请求，不启动真实 HTTP 服务。
覆盖：列表 / 新建 / 详情 / 录入 GESP / 删除
"""
import os
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 1) 在 import web_app 之前清掉 admin env（因为现有 .env 有强凭据，但 test_client 不需要登录）
# 2) 强制 .env 加载
os.environ.setdefault("TASK_DB_PATH", str(ROOT / "tasks.db"))

sys.path.insert(0, str(ROOT))

import web_app  # noqa: E402

client = web_app.app.test_client()


def banner(s):
    print()
    print("=" * 60)
    print(f"  {s}")
    print("=" * 60)


def section(s):
    print()
    print(f"--- {s} ---")


# 0) 模拟 admin 登录（直接写 session）
with client.session_transaction() as sess:
    sess["admin_authed"] = True
    sess["admin_user"] = "admin"

banner("1. 列表页（应渲染 + 含若干 demo 学员）")
r = client.get("/admin/students")
assert r.status_code == 200, r.status_code
body = r.get_data(as_text=True)
assert "学员档案" in body
assert "合计" in body  # "合计 N" 标题始终存在
print(f"  [OK] GET /admin/students status=200 + 标题渲染")

banner("2. 新建学员（GET 显示表单）")
r = client.get("/admin/students/new")
assert r.status_code == 200
assert "新建学员" in r.get_data(as_text=True)
assert "Luogu UID" in r.get_data(as_text=True)
print(f"  [OK] GET /admin/students/new 表单渲染")

banner("3. 新建学员（POST 提交）")
# 用唯一 UID 避免与 demo 学员冲突
import time as _time
test_uid = f"test_{int(_time.time())}"
r = client.post("/admin/students/new", data={
    "luogu_uid": test_uid,
    "real_name": "测试学员·甲",
    "school": "测试小学",
    "grade": "2024",
    "is_minor": "0",
    "note": "preview 阶段测试数据",
}, follow_redirects=False)
assert r.status_code == 302, f"未跳转: {r.status_code}"
loc = r.headers.get("Location", "")
assert "/admin/students/" in loc, f"Location 异常: {loc}"
sid = int(loc.split("/admin/students/")[1].split("?")[0])
print(f"  [OK] POST /admin/students/new 302 → /admin/students/{sid} (uid={test_uid})")

banner("4. 列表页（含新学员）")
r = client.get("/admin/students")
body = r.get_data(as_text=True)
assert test_uid in body
assert "测试学员·甲" in body
print(f"  [OK] 列表含新学员 UID + 姓名")

banner("5. 详情页（无 GESP）")
r = client.get(f"/admin/students/{sid}")
body = r.get_data(as_text=True)
assert r.status_code == 200
assert "暂无 GESP 真考记录" in body
assert "[1 ]" in body  # 段位图第一格空
print(f"  [OK] 详情页：基本信息 + 空段位图 + 引导录入")

banner("6. 录入 GESP 7 级 85 分")
# 找一个 L7-8 赛事 id
import sqlite3
conn = sqlite3.connect(str(ROOT / "tasks.db"))
conn.row_factory = sqlite3.Row
g7 = conn.execute(
    "SELECT id FROM competitions WHERE type='gesp' AND code LIKE '%L7-8%' LIMIT 1"
).fetchone()
g8 = conn.execute(
    "SELECT id FROM competitions WHERE type='gesp' AND code LIKE '%L5-8%' LIMIT 1"
).fetchone()
conn.close()
assert g7 and g8 and g7["id"] != g8["id"], "缺 GESP 赛事，先跑 import_competitions"

r = client.post(f"/admin/students/{sid}/gesp/new", data={
    "exam_id": str(g7["id"]),
    "registered_level": "7",
    "actual_score": "85",
    "certificate_no": "GESP-2026-09-00001",
    "notes": "preview 测试",
}, follow_redirects=False)
assert r.status_code == 302, r.status_code
print(f"  [OK] 录入 7 级 85 分：302 → {r.headers.get('Location')}")

banner("7. 录入 GESP 8 级 80 分（应触发 csp_j + csp_s 双免）")
r = client.post(f"/admin/students/{sid}/gesp/new", data={
    "exam_id": str(g8["id"]),
    "registered_level": "8",
    "actual_score": "80",
}, follow_redirects=False)
assert r.status_code == 302
print(f"  [OK] 录入 8 级 80 分：302 → {r.headers.get('Location')}")

banner("8. 详情页（2 次 GESP 记录 + 双免标识）")
r = client.get(f"/admin/students/{sid}")
body = r.get_data(as_text=True)
assert r.status_code == 200
assert "[7✦]" in body, "段位图应含 [7✦]"
assert "[8✦]" in body, "段位图应含 [8✦]"
assert "CSP-J + CSP-S 双免" in body, "应显示双免徽章"
assert "GESP 8 级" in body, "应显示最高已过 GESP 8 级"
print(f"  [OK] 详情页渲染：段位图 [7✦]──[8✦] + 双免徽章 + 2 条考试记录")

banner("9. 列表页（学员已有 GESP 元数据）")
r = client.get("/admin/students")
body = r.get_data(as_text=True)
assert "GESP 8 级" in body, "应显示最高 GESP 8 级"
assert "J+S 免" in body, "应显示 J+S 免"
print(f"  [OK] 列表显示学员 GESP 8 级 + J+S 免")

banner("10. 删除学员（POST）")
r = client.post(f"/admin/students/{sid}/delete", follow_redirects=False)
assert r.status_code == 302
print(f"  [OK] POST /admin/students/{sid}/delete 302")

banner("11. 删除后学员不存在")
r = client.get(f"/admin/students/{sid}")
assert r.status_code == 302, "删除后访问应 302 跳回列表"
print(f"  [OK] 删除后 GET /admin/students/{sid} → 302 跳列表")

print()
print("=" * 60)
print("[OK] 全部 11 项端到端测试通过")
print("=" * 60)
