"""
任务记录持久化存储模块 (SQLite)
重启服务后任务记录不会丢失
"""

import os
import sqlite3
import json
from pathlib import Path
from datetime import datetime
from typing import Optional

# 允许通过环境变量覆盖，便于 Docker 命名卷挂目录场景
# 默认：当前目录下 tasks.db（开发用）
# Docker：通过 TASK_DB_PATH=/app/data/tasks.db 把 db 文件写到挂载的卷里
DB_PATH = Path(os.environ.get("TASK_DB_PATH", "tasks.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ----------------------------------------------------------------------------
# 单一可信源：tasks 表除 task_id 外的所有列定义
# ----------------------------------------------------------------------------
# 加新列只需要在这里加一行，init_db() 启动时自动扫 PRAGMA table_info，
# 缺啥补啥（ALTER TABLE ADD COLUMN）。无需再手动维护 ALTER 列表。
#
# 值是完整的 SQL 类型段（含 NOT NULL / DEFAULT）。
#   - "TEXT DEFAULT ''"          → 字符串类字段（默认空串）
#   - "INTEGER DEFAULT 0"        → 计数 / 进度类字段
#   - "TEXT NOT NULL DEFAULT 'queued'" → 状态字段
# ----------------------------------------------------------------------------
TASK_COLUMNS: dict[str, str] = {
    "status":                "TEXT NOT NULL DEFAULT 'queued'",
    "message":               "TEXT DEFAULT ''",
    "html":                  "TEXT DEFAULT ''",
    "pdf":                   "TEXT DEFAULT ''",
    "md":                    "TEXT DEFAULT ''",
    "student_name":          "TEXT DEFAULT ''",
    "school":                "TEXT DEFAULT ''",
    "grade":                 "TEXT DEFAULT ''",
    "solved_count":          "INTEGER DEFAULT 0",
    "failed_count":          "INTEGER DEFAULT 0",
    "eval_time":             "TEXT DEFAULT ''",
    "stage":                 "TEXT DEFAULT ''",
    "source_code_success":   "INTEGER DEFAULT 0",
    "source_code_total":     "INTEGER DEFAULT 0",
    "ai_progress":           "INTEGER DEFAULT 0",
    "ai_elapsed_seconds":    "INTEGER DEFAULT 0",
    "tag_fetch_success":     "INTEGER DEFAULT 0",
    "tag_fetch_total":       "INTEGER DEFAULT 0",
    "retry_form_json":       "TEXT DEFAULT ''",
    "created_at":            "TEXT DEFAULT ''",
}


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _build_create_table_sql() -> str:
    """根据 TASK_COLUMNS 生成完整的 CREATE TABLE 语句（首次部署用）"""
    cols = ["task_id TEXT PRIMARY KEY"]
    cols.extend(f"{name} {typedef}" for name, typedef in TASK_COLUMNS.items())
    body = ",\n            ".join(cols)
    return f"CREATE TABLE IF NOT EXISTS tasks (\n            {body}\n        )"


def _ensure_columns(conn: sqlite3.Connection) -> list[str]:
    """对比 PRAGMA table_info 与 TASK_COLUMNS，对缺失列执行 ALTER TABLE ADD COLUMN。
    返回本次新加的列名列表。"""
    actual = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    added: list[str] = []
    for name, typedef in TASK_COLUMNS.items():
        if name not in actual:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {typedef}")
            added.append(name)
    return added


def init_db():
    """初始化 / 迁移数据库表。

    流程：
    1. CREATE TABLE IF NOT EXISTS（全新部署直接拿到完整 schema）
    2. 用 PRAGMA table_info 扫实际列，对比 TASK_COLUMNS，缺啥补啥
    3. 打印迁移日志，方便排查
    """
    conn = _get_conn()
    conn.execute(_build_create_table_sql())
    added = _ensure_columns(conn)
    if added:
        # 写 stdout，docker logs 能直接看到
        print(
            f"[task_store] auto-migrate: added columns to tasks table: {added}",
            flush=True,
        )
    conn.commit()
    conn.close()


def list_columns() -> list[str]:
    """返回 tasks 表当前的真实列名（供调试 / 健康检查）"""
    conn = _get_conn()
    rows = conn.execute("PRAGMA table_info(tasks)").fetchall()
    conn.close()
    return [row["name"] for row in rows]


def insert_task(task_id: str, status: str = "queued", message: str = "排队中..."):
    conn = _get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO tasks (task_id, status, message, created_at) VALUES (?, ?, ?, ?)",
        (task_id, status, message, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()


def update_task(task_id: str, **kwargs):
    """更新任务字段，支持 status, message, html, pdf, md 等"""
    conn = _get_conn()
    fields = []
    values = []
    for k, v in kwargs.items():
        fields.append(f"{k} = ?")
        values.append(v)
    if not fields:
        conn.close()
        return
    values.append(task_id)
    sql = f"UPDATE tasks SET {', '.join(fields)} WHERE task_id = ?"
    conn.execute(sql, values)
    conn.commit()
    conn.close()


def get_task(task_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    return dict(row)


def list_tasks() -> list[dict]:
    """列出所有任务，按时间倒序"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM tasks ORDER BY eval_time DESC, created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    """获取统计数字"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    conn = _get_conn()
    total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    today = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE eval_time LIKE ? OR created_at LIKE ?",
        (f"{today_str}%", f"{today_str}%"),
    ).fetchone()[0]
    running = conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'running'").fetchone()[0]
    error = conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'error'").fetchone()[0]
    conn.close()
    return {
        "total": total,
        "today": today,
        "running": running,
        "error": error,
    }


# 初始化
init_db()
