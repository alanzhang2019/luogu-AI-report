"""
activation_codes.py — v3.5 Phase 2/3

4 SKU 激活码（admin 后台生成 + 学员/家长手动兑换）。
v3.5 §0.4 + §6.2 4 SKU 商业化：
  - student_pro    ¥15/月  学员 Pro 段位 + 错题本 + StudyMate 跳转
  - parent_sub     ¥30/月  家长订阅：周报 + 倒推 + 政策
  - popularize_camp ¥99/4周  普及组冲刺营（7 级 80+ / 8 级 60+ 目标）
  - improve_camp   ¥299/8周  提高组冲刺营（8 级 80+ 目标）

本版本只实现 CRUD + 兑换，**不接支付**，按 v3.5 反向 Scope：
  - ❌ 在线支付 / 自动续费 / 退款
  - 流程：人工收款 → admin 后台生成码 → 学员/家长兑换
"""
from __future__ import annotations

import secrets
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from task_store import _get_conn  # noqa: E402

# SKU 定义：code → (中文名, 价格 CNY, 有效期天数)
SKU_CATALOG: dict[str, dict] = {
    "student_pro": {
        "name": "学员 Pro",
        "price_cny": 15,
        "duration_days": 30,
        "description": "学员 Pro 段位 + 错题本 + StudyMate 跳转 + 智能报名建议",
    },
    "parent_sub": {
        "name": "家长订阅",
        "price_cny": 30,
        "duration_days": 30,
        "description": "家长周报 + 倒推计划 + 政策日历 + 升学对照",
    },
    "popularize_camp": {
        "name": "普及组冲刺营",
        "price_cny": 99,
        "duration_days": 28,
        "description": "目标 GESP 7 级 80+ / 8 级 60+ → 解锁 9 月 CSP-J 免初赛",
    },
    "improve_camp": {
        "name": "提高组冲刺营",
        "price_cny": 299,
        "duration_days": 56,
        "description": "目标 GESP 8 级 80+ → 解锁 9 月 CSP-S 免初赛",
    },
}


# ========== 激活码 CRUD ==========

def generate_codes(
    sku: str,
    count: int = 1,
    *,
    student_id: int | None = None,
    created_by: str = "admin",
) -> list[str]:
    """
    批量生成激活码。返回 code 列表（prefix + 8 位 base32 随机）。

    prefix 设计：
      - SP   student_pro
      - PS   parent_sub
      - PJC  popularize_camp  (popularize jump camp)
      - IJC  improve_camp     (improve jump camp)
    """
    if sku not in SKU_CATALOG:
        raise ValueError(f"未知 SKU: {sku}, 必须是 {list(SKU_CATALOG.keys())} 之一")
    if not (1 <= int(count) <= 1000):
        raise ValueError("count 必须在 1-1000")

    prefix_map = {
        "student_pro": "SP",
        "parent_sub": "PS",
        "popularize_camp": "PJC",
        "improve_camp": "IJC",
    }
    prefix = prefix_map[sku]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    codes: list[str] = []
    rows: list[tuple] = []
    for _ in range(int(count)):
        # 8 位 base32 随机（不含易混淆的 0/O/1/I）
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        body = "".join(secrets.choice(alphabet) for _ in range(8))
        code = f"{prefix}-{body}"
        codes.append(code)
        rows.append((code, sku, SKU_CATALOG[sku]["duration_days"],
                    int(student_id) if student_id else None, created_by, now))

    conn = _get_conn()
    try:
        conn.executemany(
            """
            INSERT INTO activation_codes
                (code, sku, duration_days, student_id, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return codes


def list_codes(
    *,
    sku: str | None = None,
    redeemed: bool | None = None,
    student_id: int | None = None,
    limit: int = 100,
) -> list[dict]:
    conn = _get_conn()
    try:
        sql = "SELECT * FROM activation_codes WHERE 1=1"
        params: list = []
        if sku:
            sql += " AND sku = ?"
            params.append(sku)
        if student_id is not None:
            sql += " AND student_id = ?"
            params.append(int(student_id))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
        out = [dict(r) for r in rows]
    finally:
        conn.close()
    if redeemed is not None:
        out = [r for r in out if bool(r.get("redeemed_at")) == bool(redeemed)]
    return out


def get_code(code: str) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute("SELECT * FROM activation_codes WHERE code = ?", (str(code).strip(),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def redeem_code(code: str, student_id: int) -> dict:
    """
    兑换激活码：
      - 校验：code 存在 + 未过期 + 未兑换
      - 写 redeemed_at + expires_at
    返回更新后的 row
    """
    code = str(code or "").strip()
    if not code:
        raise ValueError("激活码不能为空")
    if int(student_id) <= 0:
        raise ValueError("student_id 无效")

    conn = _get_conn()
    try:
        row = conn.execute("SELECT * FROM activation_codes WHERE code = ?", (code,)).fetchone()
        if not row:
            raise ValueError(f"激活码 {code} 不存在")
        r = dict(row)
        if r.get("redeemed_at"):
            raise ValueError(f"激活码 {code} 已被兑换（{r['redeemed_at']}）")
        if r.get("student_id") and int(r["student_id"]) != int(student_id):
            # 已绑定特定学员 → 必须匹配（v3.5 §6.2 冲刺营模式）
            raise ValueError(f"激活码已绑定学员 #{r['student_id']}，无法兑换给 #{student_id}")
        # 写回
        now = datetime.now()
        expires = now + timedelta(days=int(r["duration_days"]))
        conn.execute(
            """
            UPDATE activation_codes
            SET redeemed_at = ?, expires_at = ?, student_id = ?
            WHERE id = ?
            """,
            (now.strftime("%Y-%m-%d %H:%M:%S"),
             expires.strftime("%Y-%m-%d %H:%M:%S"),
             int(student_id), int(r["id"])),
        )
        conn.commit()
    finally:
        conn.close()
    return get_code(code)


def revoke_code(code: str) -> bool:
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE activation_codes SET redeemed_at = NULL, expires_at = NULL WHERE code = ?",
            (str(code).strip(),),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def is_code_active(code: str) -> bool:
    """激活码是否处于有效期内（已兑换 + 未过期）"""
    r = get_code(code)
    if not r or not r.get("redeemed_at") or not r.get("expires_at"):
        return False
    try:
        return datetime.now() < datetime.strptime(str(r["expires_at"]), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False


def list_active_subscriptions(student_id: int) -> list[dict]:
    """学员当前所有生效中的订阅/冲刺营"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT * FROM activation_codes
            WHERE student_id = ?
              AND redeemed_at IS NOT NULL
              AND expires_at IS NOT NULL
              AND expires_at >= ?
            ORDER BY expires_at DESC
            """,
            (int(student_id), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# -- smoke test --
if __name__ == "__main__":
    import os
    if "TASK_DB_PATH" not in os.environ:
        os.environ["TASK_DB_PATH"] = str(ROOT / "tasks.db")

    print("[SMOKE] activation_codes.py")

    # 准备测试学员
    import admin_students  # noqa: E402
    test_uid = "test_act_smoke_001"
    existing = admin_students.get_student_by_uid(test_uid)
    if existing:
        admin_students.delete_student(existing["id"])
    sid = admin_students.create_student(test_uid, real_name="激活码测试")

    # 1) 生成家长订阅码
    codes1 = generate_codes("parent_sub", count=2, created_by="smoke")
    assert len(codes1) == 2
    assert all(c.startswith("PS-") for c in codes1)
    print(f"  [OK] generate_codes parent_sub: {codes1}")

    # 2) 生成冲刺营码（绑定学员）
    codes2 = generate_codes("popularize_camp", count=1, student_id=int(sid), created_by="smoke")
    assert codes2[0].startswith("PJC-")
    print(f"  [OK] generate_codes popularize_camp → student {sid}: {codes2}")

    # 3) list_codes
    all_codes = list_codes(limit=100)
    assert len(all_codes) >= 3
    ps_codes = list_codes(sku="parent_sub")
    assert all(c["sku"] == "parent_sub" for c in ps_codes)
    print(f"  [OK] list_codes: 共 {len(all_codes)} 条，parent_sub {len(ps_codes)} 条")

    # 4) 兑换家长订阅（未绑定学员）
    r1 = redeem_code(codes1[0], int(sid))
    assert r1["student_id"] == int(sid)
    assert r1["redeemed_at"] is not None
    assert r1["expires_at"] is not None
    print(f"  [OK] redeem_code: {codes1[0]} → student {sid}, expires {r1['expires_at']}")

    # 5) 重复兑换 → 失败
    try:
        redeem_code(codes1[0], int(sid))
    except ValueError as e:
        print(f"  [OK] 重复兑换被拒: {e}")
    else:
        raise AssertionError("应拒绝重复兑换")

    # 6) 兑换冲刺营码
    r2 = redeem_code(codes2[0], int(sid))
    assert r2["sku"] == "popularize_camp"
    assert r2["expires_at"] is not None
    print(f"  [OK] redeem_code camp: {codes2[0]} → expires {r2['expires_at']}")

    # 7) 错学员兑换绑定码 → 失败
    # 找另一个学员
    test_uid2 = "test_act_smoke_002"
    existing2 = admin_students.get_student_by_uid(test_uid2)
    if existing2:
        admin_students.delete_student(existing2["id"])
    sid2 = admin_students.create_student(test_uid2, real_name="另一个")
    try:
        redeem_code(codes2[0], int(sid2))  # codes2[0] 已绑定 sid
    except ValueError as e:
        print(f"  [OK] 错学员兑换绑定码被拒: {e}")
    else:
        raise AssertionError("应拒绝错学员兑换")

    # 8) is_code_active
    assert is_code_active(codes1[0])
    assert not is_code_active(codes1[1])  # 未兑换
    print(f"  [OK] is_code_active: 已兑换=True, 未兑换=False")

    # 9) list_active_subscriptions
    subs = list_active_subscriptions(int(sid))
    assert len(subs) == 2
    print(f"  [OK] list_active_subscriptions: 学员 {sid} 当前 2 个生效订阅")

    # 10) 非法 SKU
    try:
        generate_codes("invalid_sku", count=1)
    except ValueError as e:
        print(f"  [OK] 非法 SKU 被拒: {e}")

    # 清理
    admin_students.delete_student(int(sid))
    admin_students.delete_student(int(sid2))
    print(f"  [OK] 清理完毕")

    print("[OK] activation_codes smoke test passed")
