# -*- coding: utf-8 -*-
"""clear_done_tasks.py · 一键清除 done/partial 任务，解除 24h 限流

用途：
  上次部署误删 /home/ubuntu/luogu-ai-report/reports/ 后，
  tasks.db 里 17+ 个 UID 的 done 任务还在 → 24h 限流仍会拦截重跑。
  跑这个脚本一次性清掉所有 done/partial 记录，让学员能立即重新生成。

⚠️ 高危脚本（会物理删除数据库记录），默认 dry-run，需要 --yes 才真删。

用法（在服务器 / 容器内执行）：
  # 1) 预览（不删任何数据）
  python3 clear_done_tasks.py

  # 2) 真删（带 -y / --yes 确认）
  python3 clear_done_tasks.py --yes

  # 3) 只清指定 UID（逗号分隔）
  python3 clear_done_tasks.py --uids 1415258,988038,1027418 --yes

  # 4) 删除前先导出 JSON 备份到 /tmp/tasks_backup_<时间戳>.json
  python3 clear_done_tasks.py --backup --yes

容器内推荐执行：
  docker exec luogu-ai-report-luogu-coach python3 /app/clear_done_tasks.py
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

# 数据库路径（容器内固定）
DB_PATH = "/app/data/tasks.db"


def _connect():
    if not os.path.exists(DB_PATH):
        print(f"❌ 找不到数据库：{DB_PATH}")
        print("   确认在容器内执行（docker exec ... python3 /app/clear_done_tasks.py）")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _list_targets(conn, uids_filter):
    """列出所有会受影响的任务（status IN done/partial）"""
    sql = "SELECT task_id, status, luogu_uid, student_name, created_at, html, pdf, md FROM tasks"
    params = []
    if uids_filter:
        placeholders = ",".join("?" * len(uids_filter))
        sql += f" WHERE status IN ('done','partial') AND luogu_uid IN ({placeholders})"
        params = uids_filter
    else:
        sql += " WHERE status IN ('done','partial')"
    sql += " ORDER BY luogu_uid, created_at DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _summary_by_uid(rows):
    """按 UID 汇总"""
    by_uid = {}
    for r in rows:
        uid = r["luogu_uid"] or "(空)"
        by_uid.setdefault(uid, []).append(r)
    return by_uid


def _do_delete(conn, task_ids):
    """物理删除（绕过 24h 限流）"""
    deleted = 0
    for tid in task_ids:
        cur = conn.execute("DELETE FROM tasks WHERE task_id = ?", (tid,))
        deleted += cur.rowcount
    conn.commit()
    # 顺便 VACUUM 释放空间
    try:
        conn.execute("VACUUM")
    except Exception:
        pass
    return deleted


def main():
    ap = argparse.ArgumentParser(
        description="一键清除 done/partial 任务，解除 24h 限流（高危）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--yes", "-y", action="store_true", help="确认删除（不传则 dry-run）")
    ap.add_argument(
        "--uids", "-u", default="", help="只清指定 UID，逗号分隔（默认清所有 done/partial）"
    )
    ap.add_argument(
        "--backup", "-b", action="store_true", help="删除前先导出 JSON 备份到 /tmp/"
    )
    ap.add_argument(
        "--include-orphan", action="store_true", help="包含 luogu_uid 为空的孤儿任务"
    )
    args = ap.parse_args()

    uids_filter = [s.strip() for s in args.uids.split(",") if s.strip()] if args.uids else []

    print("=" * 70)
    print(f"  clear_done_tasks.py  ·  {'【DRY-RUN】' if not args.yes else '【真删】'}")
    print("=" * 70)
    print(f"  DB:           {DB_PATH}")
    print(f"  目标状态:     done, partial")
    if uids_filter:
        print(f"  限定 UID:     {', '.join(uids_filter)} ({len(uids_filter)} 个)")
    else:
        print(f"  限定 UID:     （全部）")
    if args.include_orphan:
        print(f"  含孤儿任务:   是")
    print()

    conn = _connect()
    try:
        rows = _list_targets(conn, uids_filter)
        # 过滤掉空 UID（除非指定了 --include-orphan 且没有限定 UID）
        if not uids_filter and not args.include_orphan:
            before = len(rows)
            rows = [r for r in rows if r["luogu_uid"]]
            skipped = before - len(rows)
            if skipped:
                print(f"ℹ️  跳过 {skipped} 条 luogu_uid 为空的孤儿任务（用 --include-orphan 强制包含）")
                print()

        if not rows:
            print("✅ 没有需要清除的 done/partial 任务")
            return

        by_uid = _summary_by_uid(rows)
        print(f"📋 待处理 {len(rows)} 条任务，涉及 {len(by_uid)} 个 UID：")
        print()
        print(f"  {'UID':<12} {'数量':<6} {'最近状态':<10} {'最近时间':<20} {'姓名'}")
        print("  " + "-" * 66)
        for uid in sorted(by_uid.keys(), key=lambda x: -len(by_uid[x])):
            tasks = by_uid[uid]
            latest = max(tasks, key=lambda t: t["created_at"] or "")
            print(
                f"  {uid:<12} {len(tasks):<6} {latest['status']:<10} "
                f"{(latest['created_at'] or '-'):<20} {latest['student_name'] or '-'}"
            )
        print()

        if not args.yes:
            print("=" * 70)
            print("⚠️  DRY-RUN 模式：未删除任何数据。")
            print("   确认要删除上述任务？加 --yes 重新执行：")
            print("   ", " ".join(sys.argv + ["--yes"]))
            print("=" * 70)
            return

        # 备份
        if args.backup:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = f"/tmp/tasks_backup_{ts}.json"
            with open(backup_path, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, indent=2)
            print(f"💾 备份已导出：{backup_path}  ({os.path.getsize(backup_path) // 1024} KB)")
            print()

        # 真删
        task_ids = [r["task_id"] for r in rows]
        deleted = _do_delete(conn, task_ids)
        print(f"🗑  已删除 {deleted} 条 done/partial 任务")
        print()

        # 验证
        remaining = conn.execute(
            "SELECT count(*) FROM tasks WHERE status IN ('done','partial')"
        ).fetchone()[0]
        print(f"✅ 删除后剩余 done/partial 任务：{remaining} 条")
        print()
        print("=" * 70)
        print("  24h 限流已解除！上述 UID 现在可立即重跑报告。")
        print("  重跑方式：")
        print("    · 用户自己上 https://oi.aijiangti.cn/generate-form 重新提交")
        print("    · 或者跑 bulk_regen.py（需要每人的 cookies）")
        print("=" * 70)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
