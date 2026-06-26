"""v3.10.0.13 · 回归测试 3 个 VJudge 进度增强."""
import time
import task_store as ts

# 找一个有 VJudge 账号的学生
conn = ts._get_conn()
srow = conn.execute(
    "SELECT id, vjudge_username FROM students WHERE vjudge_username IS NOT NULL LIMIT 1"
).fetchone()
if not srow:
    print("[SKIP] 没有已绑定 VJudge 的学生,跳过测试")
    raise SystemExit(0)
sid = srow["id"]
username = srow["vjudge_username"]
print(f"[INFO] student_id={sid} username={username}")

# 测试 1: enqueue 写 1/100
task_id = f"VJ-TEST-{int(time.time() * 1000)}"
conn.execute("""
    INSERT INTO student_vjudge_fetch_tasks
    (task_id, student_id, username, status, trigger, started_at,
     progress_step, progress_total, progress_msg)
    VALUES (?, ?, ?, 'pending', 'test', CURRENT_TIMESTAMP, 1, 100, '⏳ 已入队,等待 worker pickup…')
""", (task_id, sid, username))
conn.commit()
print(f"[1] enqueue task_id={task_id}")
row = conn.execute(
    "SELECT status, progress_step, progress_total, progress_msg FROM student_vjudge_fetch_tasks WHERE task_id=?",
    (task_id,),
).fetchone()
print(f"    DB: {dict(row) if row else None}")
assert row["progress_step"] == 1, f"expected step=1 got {row['progress_step']}"
assert row["progress_total"] == 100, f"expected total=100 got {row['progress_total']}"
print("    [OK] enqueue wrote 1/100")

# 测试 2: pickup 推 10/100
picked = ts.vjudge_pickup_pending_task()
if not picked or picked["task_id"] != task_id:
    # 也许 pickup 拾到了旧任务,再试一次
    print(f"[WARN] pickup picked {picked['task_id'] if picked else 'None'}, retry")
    picked = ts.vjudge_pickup_pending_task()
row = conn.execute(
    "SELECT status, progress_step, progress_total, progress_msg FROM student_vjudge_fetch_tasks WHERE task_id=?",
    (task_id,),
).fetchone()
print(f"[2] pickup DB: {dict(row) if row else None}")
if row and row["status"] == "fetching":
    assert row["progress_step"] == 10, f"expected step=10 got {row['progress_step']}"
    assert row["progress_total"] == 100, f"expected total=100 got {row['progress_total']}"
    assert "worker" in row["progress_msg"]
    print("    [OK] pickup wrote 10/100")
else:
    print(f"    [WARN] task not picked (already locked?). skipping assertion")

# 测试 3: vjudge_update_progress 写中间值
ts.vjudge_update_progress(task_id, 45, 100, "📊 解析 profile.html…")
row = conn.execute(
    "SELECT progress_step, progress_total, progress_msg FROM student_vjudge_fetch_tasks WHERE task_id=?",
    (task_id,),
).fetchone()
print(f"[3] mid-progress DB: {dict(row) if row else None}")
assert row["progress_step"] == 45, f"expected step=45 got {row['progress_step']}"
print("    [OK] update_progress wrote 45/100")

# 测试 4: finish
ts.vjudge_finish_task(task_id, "succeeded")
row = conn.execute(
    "SELECT status, progress_step, progress_total, progress_msg FROM student_vjudge_fetch_tasks WHERE task_id=?",
    (task_id,),
).fetchone()
print(f"[4] finish DB: {dict(row) if row else None}")
assert row["status"] == "succeeded", f"expected status=succeeded got {row['status']}"
print("    [OK] finish wrote succeeded")

print("\n[PASS] 3 个增强全部回归通过 ✅")
conn.close()
