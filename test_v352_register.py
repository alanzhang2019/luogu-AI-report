"""
test_v352_register.py — v3.5.2 学员 4 字段极简注册（学而思图 1 模式）

覆盖：
  1. /register GET 200 + 表单 4 字段（城市/姓名/年级/性别）+ 洛谷 UID
  2. /register POST 缺字段 → 400 + 错误提示
  3. /register POST 完整合法 → 302 → /me/<luogu_uid> 200
  4. /me/<luogu_uid> 学员档案正确显示（city/gender/grade/registered_via）
  5. 14 岁以下 + 无手机号 → 拒绝（v3.5.2 PIPL §5.2 强制）
  6. 14 岁以下 + 有手机号 → 成功（is_minor=1 + phone 已脱敏入库）
  7. luogu_uid 重复 → 错误提示
  8. gender 非法 → ValueError
  9. 微信 openid + 手机号 同时填 → 拒绝（二选一）
 10. 微信扫码桩：模拟前端注入 openid 即可
 11. /me/<未注册> → 404
 12. v3.5.2 升级：城市按省份分组（optgroup）+ 年级覆盖小学一年级~大四

执行：python test_v352_register.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
import os
os.environ.setdefault("ALLOW_INSECURE_DEFAULT", "1")

import web_app  # noqa: E402
import admin_students  # noqa: E402


def banner(s):
    print(f"\n{'='*60}\n  {s}\n{'='*60}")


def gen_uid() -> str:
    """生成 9 位纯数字 UID（用纳秒时间戳后 9 位）"""
    return str(time.time_ns())[-9:]


def main():
    client = web_app.app.test_client()

    # 1. GET /register
    banner("1. GET /register 表单渲染")
    r = client.get("/register")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    for kw in ["学员注册", "城市", "姓名", "年级", "性别", "洛谷 UID", "PIPL", "v3.5.2"]:
        assert kw in body, f"表单缺关键字段：{kw}"
    # 验证 v3.5.2 升级：optgroup 省份分组
    for province in ["直辖市", "港澳台", "广东", "江苏", "新疆", "西藏"]:
        assert f"📍 {province}" in body, f"省份分组缺：{province}"
    # 验证 v3.5.2 升级：完整学制（小学一年级 → 大四）
    for grade in ["小学一年级", "初一", "高一", "大一", "大四", "已毕业"]:
        assert grade in body, f"年级缺：{grade}"
    # 验证港澳台城市
    for city in ["香港", "澳门", "台北", "高雄"]:
        assert city in body, f"港澳台城市缺：{city}"
    print(f"  [OK] /register 200 + 8 项关键字段 + 省份分组 + 完整学制 + 港澳台 全部命中")

    # 2. 缺字段校验
    banner("2. POST /register 缺字段 → 错误提示")
    r = client.post("/register", data={
        "city": "北京",
        "real_name": "",
        "grade": "PRIMARY_3",
        "gender": "M",
        "luogu_uid": "999000",
        "agree": "on",
    })
    body = r.get_data(as_text=True)
    assert "姓名必填" in body
    print(f"  [OK] 缺姓名被拦截")

    # 3. 合法注册 → 302 → /me
    banner("3. POST /register 合法 → 302 → /me/<uid>")
    test_uid = gen_uid()
    r = client.post("/register", data={
        "city": "杭州",
        "real_name": "v352 测试学员",
        "grade": "SENIOR_2",  # 高二
        "gender": "M",
        "luogu_uid": test_uid,
        "birth_date": "2010-06-15",
        "phone": "13800138000",
        "agree": "on",
    }, follow_redirects=False)
    assert r.status_code == 302, f"应 302，实际 {r.status_code} body={r.get_data(as_text=True)[:300]}"
    loc = r.headers.get("Location", "")
    assert f"/me/{test_uid}" in loc, f"重定向地址错：{loc}"
    print(f"  [OK] 302 → {loc}")

    # 4. /me/<uid> 200 + 数据正确
    banner("4. /me/<uid> 学员档案展示")
    r = client.get(f"/me/{test_uid}")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    # /me 模板：v3.5.2 升级后 grade 显示中文 label（如 "高二（高中二年级）"）和省份
    for kw in ["v3.5.2", "v352 测试学员", test_uid, "杭州", "男生", "高二（高中二年级）", "self_web", "浙江"]:
        assert kw in body, f"/me 缺关键字段：{kw}"
    print(f"  [OK] /me/{test_uid} 200 + city/gender/grade_label/province/registered_via 全显")

    # 5. 14 岁以下 + 无手机号 → 拒绝
    banner("5. 14 岁以下 + 无手机号 → 拒绝（PIPL §5.2）")
    r = client.post("/register", data={
        "city": "北京",
        "real_name": "未满14无手机",
        "grade": "PRIMARY_4",  # 小学四年级
        "gender": "M",
        "luogu_uid": gen_uid(),
        "birth_date": "2014-01-01",
        "agree": "on",
    })
    body = r.get_data(as_text=True)
    assert "14 岁以下" in body and "家长手机号" in body, body[:200]
    print(f"  [OK] 未满 14 无手机号被拦截")

    # 6. 14 岁以下 + 有手机号 → 成功
    banner("6. 14 岁以下 + 有手机号 → 成功入库（is_minor=1 + phone 脱敏）")
    minor_uid = gen_uid()
    r = client.post("/register", data={
        "city": "深圳",
        "real_name": "未满14有手机",
        "grade": "JUNIOR_1",  # 初一
        "gender": "F",
        "luogu_uid": minor_uid,
        "birth_date": "2014-06-15",
        "phone": "13900139000",
        "agree": "on",
    }, follow_redirects=False)
    assert r.status_code == 302, r.status_code
    stu = admin_students.get_student_by_uid(minor_uid)
    assert stu, "未找到 14 岁以下学员"
    assert stu["is_minor"] == 1, f"is_minor 应=1，实际 {stu['is_minor']}"
    assert stu["city"] == "深圳"
    assert stu["gender"] == "F"
    assert stu["birth_date"] == "2014-06-15"
    assert stu["registered_via"] == "self_web"
    assert "phone=139" in stu["note"]   # 脱敏后前 3 位
    print(f"  [OK] sid={stu['id']} is_minor=1 + city='深圳' + gender='F' + birth=2014-06-15 + phone 已脱敏")

    # 7. luogu_uid 重复
    banner("7. luogu_uid 重复 → 错误提示")
    r = client.post("/register", data={
        "city": "北京",
        "real_name": "重名重试",
        "grade": "UNIV_1",  # 大一
        "gender": "M",
        "luogu_uid": test_uid,
        "agree": "on",
    })
    body = r.get_data(as_text=True)
    assert "已注册" in body
    print(f"  [OK] 重复 UID 被拦截")

    # 8. gender 非法
    banner("8. gender 非法 → ValueError")
    try:
        admin_students.create_student(
            luogu_uid=gen_uid(),
            real_name="非法性别",
            grade="PRIMARY_1",
            gender="X",
        )
        assert False, "应 raise ValueError"
    except ValueError as e:
        assert "gender" in str(e)
    print(f"  [OK] gender='X' 被 ValueError 拒绝")

    # 9. 微信 + 手机号同时填 → 拒绝
    banner("9. 微信 + 手机号 同时填 → 拒绝（二选一）")
    r = client.post("/register", data={
        "city": "上海",
        "real_name": "微信手机并存",
        "grade": "SENIOR_3",  # 高三
        "gender": "M",
        "luogu_uid": gen_uid(),
        "phone": "13800138000",
        "wechat_openid": "demo_wx_abc12345",
        "agree": "on",
    })
    body = r.get_data(as_text=True)
    assert "二选一" in body
    print(f"  [OK] 微信+手机并存被拦截")

    # 10. 微信扫码桩（openid 注入）
    banner("10. 微信扫码桩：openid 注入即可走通")
    wechat_uid = gen_uid()
    r = client.post("/register", data={
        "city": "成都",
        "real_name": "微信扫码学员",
        "grade": "PRIMARY_5",  # 小学五年级
        "gender": "M",
        "luogu_uid": wechat_uid,
        "wechat_openid": "demo_wx_openid_xyz789",
        "agree": "on",
    }, follow_redirects=False)
    assert r.status_code == 302, r.status_code
    stu = admin_students.get_student_by_uid(wechat_uid)
    assert stu
    assert stu["registered_via"] == "wechat", f"registered_via 应=wechat，实际 {stu['registered_via']}"
    assert "wechat=demo_wx_" in stu["note"], f"note 缺 wechat 标记：{stu['note']}"
    print(f"  [OK] 微信扫码桩入库：registered_via=wechat + note 包含 wechat 前 8 位")

    # 11. /me/<未注册> → 404
    banner("11. /me/<未注册 uid> → 404")
    r = client.get("/me/not_registered_999999")
    assert r.status_code == 404
    print(f"  [OK] /me/<未注册> 404")

    # 12. v3.5.2 升级：直辖市 + 港澳台 + 大学城市（江苏苏州）
    banner("12. v3.5.2 升级：直辖市/港澳台/其他省份城市均可注册")
    for city in ["上海", "香港", "乌鲁木齐", "拉萨", "呼和浩特", "苏州", "西宁"]:
        cu = gen_uid()
        r = client.post("/register", data={
            "city": city,
            "real_name": f"v352-{city}-测试",
            "grade": "UNIV_2",  # 大二
            "gender": "M",
            "luogu_uid": cu,
            "phone": "13800138000",
            "agree": "on",
        }, follow_redirects=False)
        assert r.status_code == 302, f"{city} 注册失败：{r.status_code} {r.get_data(as_text=True)[:200]}"
        stu = admin_students.get_student_by_uid(cu)
        assert stu["city"] == city, f"城市写错：{stu['city']} != {city}"
        print(f"  [OK] {city:6s} → 302 → 落库 city={stu['city']}")

    print("\n[OK] v3.5.2 学员 4 字段极简注册全流程通过")


if __name__ == "__main__":
    main()
