"""test_task_store_migrate.py
验证 task_store 的自动迁移逻辑，覆盖三种场景：
1. 全新 DB（无表）→ CREATE 直接拿全 schema
2. 旧 DB（缺 tag_fetch_* 两列）→ 自动 ALTER
3. 完整 DB（列齐全）→ 不动
"""
import os
import sys
import sqlite3
import tempfile
import importlib

# 强制每次都用一个新的 TASK_DB_PATH
def fresh_module(tmp_db: str):
    os.environ["TASK_DB_PATH"] = tmp_db
    # 重新 import
    if "task_store" in sys.modules:
        del sys.modules["task_store"]
    return importlib.import_module("task_store")


def assert_eq(label, got, want):
    if got != want:
        print(f"  ✗ {label}: got {got!r}, want {want!r}")
        sys.exit(1)
    print(f"  ✓ {label}")


def assert_set_eq(label, got, want):
    if set(got) != set(want):
        print(f"  ✗ {label}: got {set(got)!r}, want {set(want)!r}")
        sys.exit(1)
    print(f"  ✓ {label}")


def scenario_fresh():
    print("[1/3] 全新 DB：")
    with tempfile.TemporaryDirectory() as d:
        ts = fresh_module(os.path.join(d, "fresh.db"))
        cols = ts.list_columns()
        expected = {"task_id", *ts.TASK_COLUMNS.keys()}
        assert_set_eq("列集合匹配", cols, expected)
        # 第二次跑应该幂等，不报错
        ts.init_db()
        assert_set_eq("重复 init 不抛错", ts.list_columns(), expected)


def scenario_legacy_missing_tag_fetch():
    print("[2/3] 旧 DB 缺 tag_fetch_*：")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "legacy.db")
        # 手工建一个缺 tag_fetch_* 的旧表
        c = sqlite3.connect(path)
        c.execute("""
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'queued',
                message TEXT DEFAULT '',
                created_at TEXT DEFAULT ''
            )
        """)
        c.commit()
        c.close()
        # 加载模块触发 init
        ts = fresh_module(path)
        cols = set(ts.list_columns())
        assert_eq("tag_fetch_success 自动补齐", "tag_fetch_success" in cols, True)
        assert_eq("tag_fetch_total 自动补齐", "tag_fetch_total" in cols, True)
        # 第三次 init 应该幂等
        ts.init_db()
        assert_set_eq("二次 init 列集合不变", ts.list_columns(), set(ts.TASK_COLUMNS.keys()) | {"task_id"})


def scenario_up_to_date():
    print("[3/3] 完整 DB：")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "ok.db")
        ts = fresh_module(path)
        ts.init_db()  # 第一次建表
        # 第二次 init 不应该打印迁移日志（"added columns" 不会出现在 stdout）
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            ts.init_db()
        out = buf.getvalue()
        assert_eq("无迁移日志输出", "auto-migrate" not in out, True)


if __name__ == "__main__":
    scenario_fresh()
    scenario_legacy_missing_tag_fetch()
    scenario_up_to_date()
    print("\n全部通过 ✓")
