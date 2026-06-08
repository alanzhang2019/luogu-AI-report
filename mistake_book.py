"""
mistake_book.py — v3.5 Phase 3

学员错题本（v3.5 §7 P0 学员 Pro 段位 + 错题本）：
  - 从 reports/*/export_data.json 提取 failed_items
  - 按学员 UID 聚合所有错题
  - 提供错题分类(按难度/最近提交) + 跳转 StudyMate 链接
  - 数据不写数据库，**直接聚合磁盘 JSON**（v3.5 §8 反向 Scope: 不做错题社区）

字段（来自 export_data.json failed_items[*]）：
  - problem.pid / problem.title / problem.difficulty
  - record.score / record.sourceCode / record.submitTime
  - record.detail.judgeResult.finishedCaseCount
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "docs"))

REPORTS_DIR = ROOT / "reports"


# ========== 错题聚合 ==========

def _iter_export_files() -> list[Path]:
    """遍历 reports/ 下所有 export_data.json"""
    if not REPORTS_DIR.exists():
        return []
    return sorted(REPORTS_DIR.glob("*/export_data.json"))


def _extract_failed_items(export_path: Path) -> tuple[Optional[int], list[dict]]:
    """从单个 export_data.json 提取 (luogu_uid, failed_items 列表)"""
    try:
        data = json.loads(export_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, []
    fi = data.get("failed_items", [])
    if not fi:
        return None, []
    # 学员 uid 优先取 record.user.uid
    uid = None
    for item in fi:
        u = (item.get("record", {}) or {}).get("user", {}) or {}
        if u.get("uid"):
            uid = int(u["uid"])
            break
    if uid is None:
        return None, []
    return uid, fi


def collect_all_mistakes() -> dict[int, list[dict]]:
    """
    全量扫描 reports/，按学员 UID 聚合错题。
    返回 {uid: [mistake_dict, ...]}，每条 mistake 含:
      - pid / title / difficulty / score / source_code / submit_time / finished_cases
      - report_dir (来源报告目录)
    """
    out: dict[int, list[dict]] = defaultdict(list)
    for export_path in _iter_export_files():
        uid, items = _extract_failed_items(export_path)
        if uid is None:
            continue
        for item in items:
            problem = item.get("problem", {}) or {}
            record = item.get("record", {}) or {}
            judge = (record.get("detail", {}) or {}).get("judgeResult", {}) or {}
            finished = judge.get("finishedCaseCount", 0)
            submit_time = record.get("submitTime", 0)
            try:
                submit_iso = (
                    datetime.fromtimestamp(int(submit_time)).strftime("%Y-%m-%d %H:%M")
                    if submit_time else "—"
                )
            except (ValueError, OSError):
                submit_iso = "—"
            out[uid].append({
                "pid": problem.get("pid", "—"),
                "title": problem.get("title", "—"),
                "difficulty": int(problem.get("difficulty", 0) or 0),
                "score": int(record.get("score", 0) or 0),
                "source_code": record.get("sourceCode", ""),
                "submit_time": submit_iso,
                "finished_cases": int(finished),
                "report_dir": export_path.parent.name,
            })
    return dict(out)


def get_mistake_book(luogu_uid: int, *, sort_by: str = "difficulty_desc", limit: int = 50) -> list[dict]:
    """取单个学员的错题本"""
    all_mistakes = collect_all_mistakes()
    items = all_mistakes.get(int(luogu_uid), [])
    if sort_by == "difficulty_desc":
        items = sorted(items, key=lambda x: (-x["difficulty"], -x["score"]))
    elif sort_by == "recent":
        # submit_time 字符串可粗排（同格式）
        items = sorted(items, key=lambda x: x["submit_time"], reverse=True)
    elif sort_by == "score_asc":
        items = sorted(items, key=lambda x: (x["score"], -x["difficulty"]))
    return items[:limit]


def get_mistake_stats(luogu_uid: int) -> dict:
    """错题统计概览"""
    items = get_mistake_book(luogu_uid, limit=9999)
    if not items:
        return {
            "luogu_uid": int(luogu_uid),
            "total": 0,
            "by_difficulty": {},
            "by_report": 0,
            "latest_submit": None,
        }
    by_difficulty: dict[int, int] = defaultdict(int)
    for it in items:
        by_difficulty[it["difficulty"]] += 1
    return {
        "luogu_uid": int(luogu_uid),
        "total": len(items),
        "by_difficulty": dict(sorted(by_difficulty.items(), reverse=True)),
        "by_report": len({it["report_dir"] for it in items}),
        "latest_submit": max(it["submit_time"] for it in items if it["submit_time"] != "—"),
    }


def get_top_mistakes_for_weekly_report(luogu_uid: int, limit: int = 3) -> list[dict]:
    """取周报要展示的"本周错题 Top 3"（取最近提交 + 高难度）"""
    items = get_mistake_book(luogu_uid, sort_by="recent", limit=20)
    # 再按难度排，取前 N
    return sorted(items, key=lambda x: (-x["difficulty"], -x["score"]))[:limit]


# -- smoke test --
if __name__ == "__main__":
    print("[SMOKE] mistake_book.py")

    all_mistakes = collect_all_mistakes()
    print(f"  [OK] 全量扫描: {len(all_mistakes)} 个学员有错题记录")
    total = sum(len(v) for v in all_mistakes.values())
    print(f"  [OK] 错题总数: {total} 题")
    if all_mistakes:
        # 找错题最多的学员
        top_uid = max(all_mistakes.keys(), key=lambda u: len(all_mistakes[u]))
        top_count = len(all_mistakes[top_uid])
        print(f"  [OK] 错题最多学员 UID {top_uid}: {top_count} 题")

        # 单个学员错题本
        book = get_mistake_book(top_uid, sort_by="difficulty_desc", limit=5)
        print(f"  [OK] UID {top_uid} Top 5 错题（按难度降序）:")
        for m in book:
            print(f"        {m['pid']:8s} 难度 {m['difficulty']}  {m['title'][:30]:30s} "
                  f"得分 {m['score']:3d}  {m['submit_time']}")

        # 统计
        stats = get_mistake_stats(top_uid)
        print(f"  [OK] 统计: 难度分布 = {stats['by_difficulty']}, "
              f"覆盖报告 {stats['by_report']} 份, 最近 {stats['latest_submit']}")

        # 周报 Top 3
        top3 = get_top_mistakes_for_weekly_report(top_uid, limit=3)
        print(f"  [OK] 周报 Top 3: {[m['pid'] for m in top3]}")

    # 0 个错题场景
    assert get_mistake_book(999999) == []
    assert get_mistake_stats(999999)["total"] == 0
    print(f"  [OK] 无错题学员降级正常")

    print("[OK] mistake_book smoke test passed")
