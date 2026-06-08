"""
import_competitions.py — v3.5 P0

从 docs/competitions.json 加载 2026 年 OI 赛事 + 政策日历种子数据到 SQLite。

特性：
  - 幂等：INSERT OR IGNORE，重复跑不变
  - JSON level_min/level_max → SQL level（取 min，多级别信息保留在 name/code 中）
  - 兼容 docs/competitions.json 的字段差异（target_audience 缺失时为空）

用法：
    python import_competitions.py              # 从默认路径导入
    python import_competitions.py --dry-run    # 只打印不写库
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

# 默认 DB 路径与 task_store.py 一致
DB_PATH = Path(os.environ.get("TASK_DB_PATH", "tasks.db"))
DEFAULT_JSON = Path(__file__).parent / "docs" / "competitions.json"


def _get_conn(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        sys.exit(
            f"[ERROR] DB 不存在：{db_path}\n"
            f"        请先跑 `python task_store.py` 初始化 schema"
        )
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def import_competitions(conn: sqlite3.Connection, items: list[dict]) -> int:
    """导入 competitions 表，返回新插入条数"""
    sql = """
        INSERT OR IGNORE INTO competitions
            (code, name, type, level, exam_date, registration_deadline,
             target_audience, fee_cny, source_url, data_year, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    n_inserted = 0
    for c in items:
        # level_min/level_max → level（取 min）
        # 非 GESP 赛事 level 字段为 NULL
        level = c.get("level_min")
        if level is None and c.get("type", "").startswith("gesp"):
            # 兜底：GESP 必须有级别
            print(f"  [WARN] {c.get('code')} 缺 level_min，跳过")
            continue
        try:
            cur = conn.execute(
                sql,
                (
                    c["code"],
                    c["name"],
                    c["type"],
                    level,
                    c["exam_date"],
                    c.get("registration_deadline"),
                    c.get("target_audience", ""),
                    c.get("fee_cny", 0),
                    c.get("source_url", ""),
                    c["data_year"],
                    c.get("notes", ""),
                ),
            )
            if cur.rowcount > 0:
                n_inserted += 1
        except KeyError as e:
            print(f"  [WARN] {c.get('code', '?')} 缺必填字段 {e}，跳过")
    conn.commit()
    return n_inserted


def import_policy_events(conn: sqlite3.Connection, items: list[dict]) -> int:
    """导入 policy_events 表，返回新插入条数"""
    sql = """
        INSERT OR IGNORE INTO policy_events
            (event_code, name, category, event_date, target_audience,
             source_url, description, data_year)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """
    n_inserted = 0
    for p in items:
        try:
            cur = conn.execute(
                sql,
                (
                    p["event_code"],
                    p["name"],
                    p.get("category", ""),
                    p.get("event_date"),
                    p.get("target_audience", ""),
                    p.get("source_url", ""),
                    p.get("description", ""),
                    p["data_year"],
                ),
            )
            if cur.rowcount > 0:
                n_inserted += 1
        except KeyError as e:
            print(f"  [WARN] {p.get('event_code', '?')} 缺必填字段 {e}，跳过")
    conn.commit()
    return n_inserted


def main() -> int:
    parser = argparse.ArgumentParser(description="导入 competitions.json 到 SQLite")
    parser.add_argument(
        "--json",
        default=str(DEFAULT_JSON),
        help="competitions.json 路径（默认 docs/competitions.json）",
    )
    parser.add_argument(
        "--db",
        default=str(DB_PATH),
        help="SQLite DB 路径（默认 tasks.db）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只解析 + 打印，不写库",
    )
    args = parser.parse_args()

    json_path = Path(args.json)
    if not json_path.exists():
        sys.exit(f"[ERROR] JSON 不存在：{json_path}")

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    comp_list = data.get("competitions", [])
    policy_list = data.get("policy_events", [])
    print(f"[INFO] 解析 {json_path.name}")
    print(f"       - {len(comp_list)} 个 competitions")
    print(f"       - {len(policy_list)} 个 policy_events")
    print(f"       - version: {data.get('version', '?')}")
    print(f"       - last_updated: {data.get('last_updated', '?')}")
    print(f"       - DB: {args.db}")

    if args.dry_run:
        print("[DRY-RUN] 不写库，退出")
        return 0

    # 重新解析 DB_PATH（支持 --db 覆盖）
    db_path = Path(args.db)

    conn = _get_conn(db_path)
    n_comp = import_competitions(conn, comp_list)
    n_pol = import_policy_events(conn, policy_list)

    # 统计
    cur = conn.execute("SELECT COUNT(*) FROM competitions")
    total_comp = cur.fetchone()[0]
    cur = conn.execute("SELECT COUNT(*) FROM policy_events")
    total_pol = cur.fetchone()[0]
    conn.close()

    print(f"\n[OK] import_competitions done")
    print(f"     新增 competitions: {n_comp} (DB 总数: {total_comp})")
    print(f"     新增 policy_events: {n_pol} (DB 总数: {total_pol})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
