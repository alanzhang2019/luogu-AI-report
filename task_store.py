"""
任务记录持久化存储模块 (SQLite)
重启服务后任务记录不会丢失
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime
from typing import Optional

DB_PATH = Path("tasks.db")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """初始化数据库表"""
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'queued',
            message TEXT DEFAULT '',
            html TEXT DEFAULT '',
            pdf TEXT DEFAULT '',
            md TEXT DEFAULT '',
            student_name TEXT DEFAULT '',
            school TEXT DEFAULT '',
            grade TEXT DEFAULT '',
            solved_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            eval_time TEXT DEFAULT '',
            stage TEXT DEFAULT '',
            source_code_success INTEGER DEFAULT 0,
            source_code_total INTEGER DEFAULT 0,
            ai_progress INTEGER DEFAULT 0,
            ai_elapsed_seconds INTEGER DEFAULT 0,
            retry_form_json TEXT DEFAULT '',
            created_at TEXT DEFAULT ''
        )
    """)
    # 兼容历史数据库：旧表可能缺字段，做增量迁移
    for ddl in (
        "ALTER TABLE tasks ADD COLUMN stage TEXT DEFAULT ''",
        "ALTER TABLE tasks ADD COLUMN source_code_success INTEGER DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN source_code_total INTEGER DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN ai_progress INTEGER DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN ai_elapsed_seconds INTEGER DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN retry_form_json TEXT DEFAULT ''",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


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
