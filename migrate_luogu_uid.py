"""v3.8 · 把 tasks.luogu_uid 回填到 reports/<task_id前8位>_<name>/luogu_uid.txt

用法（在项目根目录）:
    python migrate_luogu_uid.py                  # 全量回填
    python migrate_luogu_uid.py --uid 987972     # 只回填某 UID
    python migrate_luogu_uid.py --dry-run        # 只看不动

前置：reports/ 目录存在；tasks.db 存在且含 luogu_uid 列
"""
import argparse
import sqlite3
import sys
from pathlib import Path

# v3.8 · Windows PowerShell 默认 GBK 会让 emoji / 中文标点 UnicodeEncodeError
# 在脚本顶部把 stdout/stderr 强制重配为 utf-8（Linux/Mac 已是 utf-8，幂等）
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).parent
REPORTS_DIR = ROOT / "reports"
DB_PATH = ROOT / "tasks.db"


def _migrate(only_uid: str | None = None, dry_run: bool = False) -> tuple[int, int, int, int]:
    """返回 (扫描, 命中任务, 已存在txt, 新写入txt, 已存在跳过, UID空跳过)"""
    if not REPORTS_DIR.exists():
        print(f"[FAIL] reports/ 不存在: {REPORTS_DIR}")
        sys.exit(1)
    if not DB_PATH.exists():
        print(f"[FAIL] tasks.db 不存在: {DB_PATH}")
        sys.exit(1)

    # 1) 读 tasks 表全部 (task_id, luogu_uid)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT task_id, luogu_uid, student_name FROM tasks "
            "WHERE luogu_uid IS NOT NULL AND luogu_uid != ''"
        ).fetchall()
    finally:
        conn.close()

    task_uid_map: dict[str, str] = {r["task_id"][:8]: r["luogu_uid"] for r in rows}
    task_name_map: dict[str, str] = {r["task_id"][:8]: r["student_name"] for r in rows}
    print(f"📊 tasks 表有 {len(rows)} 条带 luogu_uid 的记录（按 task_id[:8] 索引）")

    if only_uid:
        task_uid_map = {k: v for k, v in task_uid_map.items() if v == only_uid}
        print(f"🔍 只回填 UID={only_uid}：{len(task_uid_map)} 条")

    # 2) 扫 reports/ 下所有目录
    scanned = 0
    written = 0
    skipped_existing = 0
    skipped_unknown = 0
    skipped_empty = 0
    print("\n=== 扫描结果 ===")
    for d in sorted(REPORTS_DIR.iterdir()):
        if not d.is_dir():
            continue
        # 目录名格式: {task_id[:8]}_{safe_name}（safe_name 已 strip alnum_-）
        name = d.name
        underscore = name.find("_")
        if underscore <= 0:
            scanned += 1
            skipped_unknown += 1
            print(f"  ⚠️  {name}  目录名格式不符（无 _）")
            continue
        task_prefix = name[:underscore]
        scanned += 1

        target_uid = task_uid_map.get(task_prefix)
        if not target_uid:
            skipped_unknown += 1
            print(f"  ⚠️  {name}  tasks 表找不到 task_id[:8]={task_prefix}（可能太老）")
            continue

        sidecar = d / "luogu_uid.txt"
        if sidecar.exists():
            cur = sidecar.read_text(encoding="utf-8", errors="replace").strip()
            if cur == target_uid:
                skipped_existing += 1
                print(f"  ✓  {name}  已有正确侧车: {cur}")
                continue
            else:
                # 已有但内容不符 → 覆盖（用户授权）
                if dry_run:
                    print(f"  🔄 {name}  DRY: 覆盖 {cur!r} → {target_uid!r}")
                else:
                    sidecar.write_text(target_uid, encoding="utf-8")
                    written += 1
                    print(f"  🔄 {name}  覆盖 {cur!r} → {target_uid!r}")
        else:
            if dry_run:
                print(f"  ➕ {name}  DRY: 写入 {target_uid}（学员 {task_name_map.get(task_prefix, '?')}）")
            else:
                sidecar.write_text(target_uid, encoding="utf-8")
                written += 1
                print(f"  ➕ {name}  写入 {target_uid}（学员 {task_name_map.get(task_prefix, '?')}）")

    print()
    print(f"=== 汇总 ===")
    print(f"扫描: {scanned} 目录")
    print(f"已写入/覆盖: {written}")
    print(f"已存在且正确: {skipped_existing}")
    print(f"tasks 表找不到对应 UID: {skipped_unknown}")
    if dry_run:
        print(f"\n⚠️  DRY-RUN 模式：未实际修改文件，去掉 --dry-run 真正写入")
    return scanned, written, skipped_existing, skipped_unknown


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--uid", default=None, help="只回填某个 UID")
    p.add_argument("--dry-run", action="store_true", help="只查看，不写入")
    args = p.parse_args()
    _migrate(only_uid=args.uid, dry_run=args.dry_run)
