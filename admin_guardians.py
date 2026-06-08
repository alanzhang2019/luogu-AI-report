"""
admin_guardians.py — v3.5 Phase 2

家长 CRUD + token 链接（30 天过期）。
基于 task_store.py 的 guardians 表。

业务规则（v3.5 §6.2 + §9 风险对冲）：
  - 家长与学员 N:1 关系（一个学员可绑多个家长：爸/妈/监护人）
  - notify_token 用于 /parent/<token> 无登录访问，必须 30 天有效
  - token 用 secrets.token_urlsafe(32) 生成（不依赖 HMAC，便于家长转发）
  - 14 岁以下学员必须先有 is_minor=1 + 至少 1 个家长 + consent_ip
  - token 过期/失效后家长访问自动 410 Gone，引导"联系教练续期"
"""
from __future__ import annotations

import secrets
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from task_store import _get_conn  # noqa: E402

# v3.5 §6.2 验收：家长 token 30 天过期
GUARDIAN_TOKEN_TTL_DAYS = 30
ALLOWED_NOTIFY_CHANNELS = {"email", "sms", "wechat", "none"}


# ========== 家长 CRUD ==========

def create_guardian(
    student_id: int,
    *,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    display_name: Optional[str] = None,
    notify_channel: str = "email",
    consent_ip: Optional[str] = None,
    token_ttl_days: int = GUARDIAN_TOKEN_TTL_DAYS,
) -> dict:
    """
    创建家长记录，自动生成 30 天有效的 notify_token。
    返回 {id, notify_token, notify_token_expires_at, ...}
    """
    if not (1 <= int(student_id) <= 999999999):
        raise ValueError("student_id 无效")
    notify_channel = (notify_channel or "email").strip().lower()
    if notify_channel not in ALLOWED_NOTIFY_CHANNELS:
        raise ValueError(f"notify_channel 必须是 {sorted(ALLOWED_NOTIFY_CHANNELS)} 之一")

    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now() + timedelta(days=int(token_ttl_days))).strftime("%Y-%m-%d %H:%M:%S")

    conn = _get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO guardians (
                student_id, phone, email, display_name, notify_channel,
                notify_token, notify_token_expires_at, consent_ip
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(student_id),
                (phone or None),
                (email or None),
                (display_name or None),
                notify_channel,
                token,
                expires_at,
                (consent_ip or None),
            ),
        )
        conn.commit()
        gid = int(cur.lastrowid)
    finally:
        conn.close()
    return {
        "id": gid,
        "student_id": int(student_id),
        "notify_token": token,
        "notify_token_expires_at": expires_at,
        "notify_channel": notify_channel,
    }


def get_guardian(guardian_id: int) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute("SELECT * FROM guardians WHERE id = ?", (int(guardian_id),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_guardian_by_token(token: str) -> dict | None:
    """根据 notify_token 拉家长记录。无 / 过期 / 错误 token → None"""
    if not token or not str(token).strip():
        return None
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM guardians WHERE notify_token = ?", (str(token).strip(),)
        ).fetchone()
        if not row:
            return None
        g = dict(row)
        # 过期校验
        expires_at = g.get("notify_token_expires_at")
        if expires_at:
            try:
                if datetime.now() > datetime.strptime(str(expires_at), "%Y-%m-%d %H:%M:%S"):
                    return None  # 过期
            except ValueError:
                return None
        return g
    finally:
        conn.close()


def list_guardians_by_student(student_id: int) -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT * FROM guardians
            WHERE student_id = ?
            ORDER BY id ASC
            """,
            (int(student_id),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_guardians_for_token_rotation(*, days_remaining_threshold: int = 7) -> list[dict]:
    """列出 7 天内即将过期的家长（供 APScheduler 提醒家长续期）"""
    threshold = (datetime.now() + timedelta(days=int(days_remaining_threshold))).strftime("%Y-%m-%d %H:%M:%S")
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT g.*, s.real_name AS student_name, s.luogu_uid
            FROM guardians g
            LEFT JOIN students s ON s.id = g.student_id
            WHERE g.notify_token_expires_at IS NOT NULL
              AND g.notify_token_expires_at <= ?
              AND g.notify_token_expires_at >= ?
            ORDER BY g.notify_token_expires_at ASC
            """,
            (threshold, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def rotate_token(guardian_id: int, token_ttl_days: int = GUARDIAN_TOKEN_TTL_DAYS) -> str:
    """重新生成 token + 续期 30 天，返回新 token"""
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now() + timedelta(days=int(token_ttl_days))).strftime("%Y-%m-%d %H:%M:%S")
    conn = _get_conn()
    try:
        conn.execute(
            """
            UPDATE guardians
            SET notify_token = ?, notify_token_expires_at = ?
            WHERE id = ?
            """,
            (token, expires_at, int(guardian_id)),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def delete_guardian(guardian_id: int) -> bool:
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM guardians WHERE id = ?", (int(guardian_id),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def has_any_guardian(student_id: int) -> bool:
    """学员是否至少绑定 1 个未过期家长（用于未成年学员合规校验）"""
    conn = _get_conn()
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM guardians
            WHERE student_id = ?
              AND (notify_token_expires_at IS NULL
                   OR notify_token_expires_at >= ?)
            """,
            (int(student_id), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchone()
        return int(row["cnt"]) > 0
    finally:
        conn.close()


# ========== 周报打开数（PIPL / 周报送达率统计用） ==========

def increment_weekly_report_open(report_id: int) -> None:
    """家长每次打开周报链接 +1（不去重，按 IP 计数）"""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE weekly_reports SET open_count = COALESCE(open_count, 0) + 1 WHERE id = ?",
            (int(report_id),),
        )
        conn.commit()
    finally:
        conn.close()


# -- smoke test --
if __name__ == "__main__":
    import os
    if "TASK_DB_PATH" not in os.environ:
        os.environ["TASK_DB_PATH"] = str(ROOT / "tasks.db")

    print("[SMOKE] admin_guardians.py")

    # 0) 准备一个测试学员
    import admin_students  # noqa: E402
    test_uid = "test_guardian_smoke_001"
    existing = admin_students.get_student_by_uid(test_uid)
    if existing:
        admin_students.delete_student(existing["id"])
    sid = admin_students.create_student(
        test_uid, real_name="测试家长绑定", school="测试小学", grade="2024"
    )
    print(f"  [OK] 准备学员 id={sid}")

    # 1) 创建家长
    g1 = create_guardian(
        student_id=sid,
        phone="13800138000",
        email="parent@example.com",
        display_name="张妈妈",
        notify_channel="email",
        consent_ip="127.0.0.1",
    )
    assert g1["notify_token"] and len(g1["notify_token"]) >= 32
    assert g1["notify_token_expires_at"]
    print(f"  [OK] create_guardian id={g1['id']} token={g1['notify_token'][:12]}...")

    # 2) by_token 读取
    g1_back = get_guardian_by_token(g1["notify_token"])
    assert g1_back and g1_back["id"] == g1["id"]
    assert g1_back["phone"] == "13800138000"
    print(f"  [OK] get_guardian_by_token 反查成功")

    # 3) list_guardians_by_student
    gs = list_guardians_by_student(sid)
    assert len(gs) == 1
    print(f"  [OK] list_guardians_by_student = {len(gs)}")

    # 4) rotate_token
    old_token = g1["notify_token"]
    new_token = rotate_token(g1["id"])
    assert new_token != old_token
    assert get_guardian_by_token(old_token) is None  # 旧的失效
    assert get_guardian_by_token(new_token) is not None
    print(f"  [OK] rotate_token: 旧 token 失效，新 token 有效")

    # 5) has_any_guardian
    assert has_any_guardian(sid)
    print(f"  [OK] has_any_guardian = True")

    # 6) 模拟过期场景：把 expires_at 改成昨天
    conn = _get_conn()
    conn.execute(
        "UPDATE guardians SET notify_token_expires_at = '2000-01-01 00:00:00' WHERE id = ?",
        (g1["id"],),
    )
    conn.commit()
    conn.close()
    assert get_guardian_by_token(new_token) is None  # 过期
    assert not has_any_guardian(sid)
    print(f"  [OK] 过期 token 自动拒绝")

    # 7) 还原 + 删除
    rotate_token(g1["id"])
    assert delete_guardian(g1["id"])
    assert get_guardian(g1["id"]) is None
    print(f"  [OK] delete_guardian")

    # 8) 清理学员
    admin_students.delete_student(sid)
    print(f"  [OK] 清理测试学员")

    print("[OK] admin_guardians smoke test passed")
