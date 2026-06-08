"""
studymate_panel.py — v3.5 Phase 3

错题 AI 讲题面板（v3.5 §7 P0 + §3.2）。

复用 docs/studymate_bridge.py 的 URL 构造器，本模块负责：
  - 错题 → StudyMate 链接列表
  - 按 GESP 等级自动匹配讲解深度
  - 学员 Pro 面板 HTML 渲染
  - 班级批量讲题（教练视角）

注意：
  - v3.5 P1 升级为 iframe 内嵌（postMessage 通信），本版本仅 URL 跳转
  - 不做错题社区（v3.5 §8 反向 Scope）
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "docs"))

from studymate_bridge import (  # noqa: E402
    build_studymate_url,
    render_studymate_button,
    STUDYMATE_BASE_URL,
)

from mistake_book import (  # noqa: E402
    get_mistake_book,
    get_mistake_stats,
    get_top_mistakes_for_weekly_report,
)
import admin_students  # noqa: E402


# ========== 难度 → 讲解深度映射 ==========
DIFFICULTY_TO_GESP_LEVEL = {
    1: 1,  # 入门 ≈ GESP 1-2 级
    2: 2,
    3: 3,
    4: 4,  # 普及 ≈ GESP 4-5
    5: 5,
    6: 6,  # 提高 ≈ GESP 6-7
    7: 8,  # 省选/NOI ≈ GESP 8+
}


def _auto_gesp_level(difficulty: int) -> int:
    return DIFFICULTY_TO_GESP_LEVEL.get(int(difficulty), 4)


# ========== 单学员面板 ==========

def build_panel_for_student(luogu_uid: int, *, limit: int = 20, sort_by: str = "difficulty_desc") -> dict:
    """
    给单个学员构造错题 AI 讲题面板数据。
    返回 {luogu_uid, items, stats, total_unannotated, generated_at}
    """
    student = admin_students.get_student_by_uid(luogu_uid)
    if not student:
        # 学员档案不存在 → 用匿名（不创建空记录）
        return {
            "luogu_uid": int(luogu_uid),
            "student": None,
            "items": [],
            "stats": {"total": 0, "by_difficulty": {}},
            "total_unannotated": 0,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "warning": f"UID {luogu_uid} 尚未在学员档案中注册（可忽略，裸数据预览可用）",
        }
    book = get_mistake_book(luogu_uid, sort_by=sort_by, limit=limit)
    items: list[dict] = []
    for m in book:
        gesp_lv = _auto_gesp_level(m["difficulty"])
        url = build_studymate_url(
            m["pid"],
            student_id=int(student["id"]),
            gesp_level=gesp_lv,
            exam_date=datetime.now().strftime("%Y-%m-%d"),
            extra={"difficulty": m["difficulty"], "score": m["score"]},
        )
        items.append({
            "pid": m["pid"],
            "title": m["title"],
            "difficulty": m["difficulty"],
            "difficulty_label": ["—", "入门", "入门", "普及-", "普及", "普及+", "提高", "省选"][min(m["difficulty"], 7)],
            "score": m["score"],
            "submit_time": m["submit_time"],
            "gesp_level": gesp_lv,
            "studymate_url": url,
            "btn_html": render_studymate_button(m["pid"], gesp_level=gesp_lv),
        })
    return {
        "luogu_uid": int(luogu_uid),
        "student": student,
        "items": items,
        "stats": get_mistake_stats(luogu_uid),
        "total_unannotated": sum(1 for m in book if m["source_code"]),  # 有源码即可讲题
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ========== 班级批量面板（教练视角） ==========

def build_batch_panel(*, top_n: int = 3, only_pro: bool = True) -> dict:
    """
    给教练的"全班 Top 错题"汇总：
      - 收集所有学员的 Top N 错题
      - 按 pid 聚合 → 出现次数最多的 = 班级共性错题
      - 返回 {by_problem: [...], students: [...], generated_at}
    """
    from mistake_book import collect_all_mistakes  # 延迟导入
    all_mistakes = collect_all_mistakes()
    # 每个学员的 Top N
    per_student: list[dict] = []
    by_pid: dict[str, dict] = {}
    for uid, items in all_mistakes.items():
        # 按 difficulty desc 取前 N
        top = sorted(items, key=lambda x: (-x["difficulty"], -x["score"]))[:int(top_n)]
        student = admin_students.get_student_by_uid(int(uid)) or {
            "id": None, "luogu_uid": str(uid), "real_name": f"UID-{uid}",
        }
        # 是否学员 Pro
        is_pro = False
        try:
            from activation_codes import list_active_subscriptions
            if student.get("id"):
                is_pro = any(
                    s.get("sku") == "student_pro"
                    for s in list_active_subscriptions(int(student["id"]))
                )
        except Exception:
            pass
        per_student.append({
            "student": student,
            "top_mistakes": top,
            "is_pro": is_pro,
        })
        for m in top:
            pid = m["pid"]
            if pid not in by_pid:
                by_pid[pid] = {
                    "pid": pid,
                    "title": m["title"],
                    "difficulty": m["difficulty"],
                    "occurrences": 0,
                    "students": [],
                }
            by_pid[pid]["occurrences"] += 1
            by_pid[pid]["students"].append(student.get("real_name") or f"UID-{uid}")
    # 班级共性错题
    common = sorted(by_pid.values(), key=lambda x: -x["occurrences"])[:10]
    return {
        "per_student": per_student,
        "common_mistakes": common,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ========== HTML 渲染 ==========

PANEL_HTML_TMPL = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>StudyMate AI 讲题面板 - {title}</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 min-h-screen p-6">
    <div class="max-w-5xl mx-auto">
        <div class="bg-gradient-to-r from-purple-700 to-blue-600 text-white rounded-xl p-6 mb-6 shadow-lg">
            <h1 class="text-2xl font-bold mb-2">🤖 StudyMate AI 讲题面板</h1>
            <p class="text-sm opacity-90">{subtitle}</p>
            <p class="text-xs opacity-75 mt-2">生成于 {generated_at} · 数据源 {STUDYMATE_BASE_URL}</p>
        </div>

        {stats_block}

        {warning_block}

        <div class="bg-white rounded-xl shadow overflow-hidden">
            <div class="px-6 py-4 border-b border-gray-200 flex items-center justify-between">
                <h2 class="text-lg font-semibold text-gray-800">📚 错题清单（{n} 题）</h2>
                <span class="text-xs text-gray-500">按难度降序</span>
            </div>
            {rows}
        </div>

        {footer}
    </div>
</body>
</html>
"""


def render_panel_html(panel: dict, *, mode: str = "student") -> str:
    """渲染面板 HTML（mode: student / batch）"""
    if mode == "student":
        student = panel.get("student")
        if student:
            title = student.get("real_name") or f"UID-{panel['luogu_uid']}"
            subtitle = f"学员 {title} · UID {student.get('luogu_uid')} · 学校 {student.get('school') or '—'}"
        else:
            title = f"UID-{panel['luogu_uid']}"
            subtitle = title

        stats = panel.get("stats", {})
        stats_html = f"""
        <div class="bg-white rounded-xl shadow p-6 mb-4 grid grid-cols-4 gap-4 text-center">
            <div><div class="text-2xl font-bold text-purple-600">{stats.get('total', 0)}</div><div class="text-xs text-gray-500">错题总数</div></div>
            <div><div class="text-2xl font-bold text-blue-600">{stats.get('by_report', 0)}</div><div class="text-xs text-gray-500">覆盖报告</div></div>
            <div><div class="text-2xl font-bold text-green-600">{panel.get('total_unannotated', 0)}</div><div class="text-xs text-gray-500">可 AI 讲题</div></div>
            <div><div class="text-2xl font-bold text-orange-600">{stats.get('by_difficulty', {}).get(7, 0)}</div><div class="text-xs text-gray-500">难度 7 题数</div></div>
        </div>
        """
        warning = ""
        if panel.get("warning"):
            warning = f'<div class="mb-4 px-4 py-3 bg-yellow-50 border border-yellow-200 text-yellow-700 rounded text-sm">⚠️ {panel["warning"]}</div>'

        rows_html = ""
        if not panel.get("items"):
            rows_html = '<div class="p-8 text-center text-gray-400">该学员暂无错题记录</div>'
        else:
            for it in panel["items"]:
                rows_html += f"""
                <div class="px-6 py-4 border-t border-gray-100 hover:bg-purple-50 transition">
                    <div class="flex items-center justify-between">
                        <div class="flex-1">
                            <div class="font-mono text-purple-700 font-semibold">{it['pid']}</div>
                            <div class="text-sm text-gray-800">{it['title']}</div>
                            <div class="text-xs text-gray-500 mt-1">
                                难度 {it['difficulty']} ({it['difficulty_label']}) ·
                                得分 {it['score']} ·
                                GESP 讲解深度 {it['gesp_level']} 级 ·
                                提交 {it['submit_time']}
                            </div>
                        </div>
                        <a href="{it['studymate_url']}" target="_blank" rel="noopener noreferrer"
                           class="ml-4 px-4 py-2 bg-purple-600 text-white rounded-lg hover:bg-purple-700 text-sm whitespace-nowrap">
                            🤖 AI 讲题
                        </a>
                    </div>
                </div>
                """
        footer = f'<p class="text-center text-xs text-gray-400 mt-8">v3.5 Phase 3 · 学员 Pro 错题本 + StudyMate 跳转 · 不含 PII</p>'
        return PANEL_HTML_TMPL.format(
            title=title, subtitle=subtitle, generated_at=panel["generated_at"],
            stats_block=stats_html, warning_block=warning,
            n=len(panel.get("items", [])),
            rows=rows_html, footer=footer,
            STUDYMATE_BASE_URL=STUDYMATE_BASE_URL,
        )

    # batch mode（教练视角）
    common = panel.get("common_mistakes", [])
    rows_html = ""
    for c in common:
        # 教练视角：按难度映射 GESP 讲解深度；student_id=None 走 anon token
        gesp_level = DIFFICULTY_TO_GESP_LEVEL.get(int(c["difficulty"]), 5)
        sm_url = build_studymate_url(
            c["pid"],
            student_id=None,
            gesp_level=gesp_level,
            extra={"mode": "coach_batch"},
        )
        rows_html += f"""
        <tr class="border-t border-gray-100">
            <td class="px-4 py-3 font-mono text-purple-700">{c['pid']}</td>
            <td class="px-4 py-3">{c['title']}</td>
            <td class="px-4 py-3 text-center">{c['difficulty']}<br><span class="text-xs text-gray-400">GESP {gesp_level}</span></td>
            <td class="px-4 py-3 text-center font-bold text-red-600">{c['occurrences']}</td>
            <td class="px-4 py-3 text-xs text-gray-500">{', '.join(c['students'][:3])}{'...' if len(c['students'])>3 else ''}</td>
            <td class="px-4 py-3 text-center">
                <a href="{sm_url}" target="_blank" rel="noopener noreferrer"
                   class="inline-block px-3 py-1.5 bg-purple-600 text-white rounded-lg hover:bg-purple-700 text-xs whitespace-nowrap">
                    🤖 AI 讲题
                </a>
            </td>
        </tr>
        """
    title = "教练班级面板"
    subtitle = f"共 {len(panel.get('per_student', []))} 名学员有错题记录"
    return PANEL_HTML_TMPL.format(
        title=title, subtitle=subtitle, generated_at=panel["generated_at"],
        stats_block="", warning_block="",
        n=len(common),
        rows=f'<table class="min-w-full text-sm"><thead class="bg-gray-50 text-gray-600 font-medium"><tr>'
             f'<th class="px-4 py-3 text-left">PID</th><th class="px-4 py-3 text-left">标题</th>'
             f'<th class="px-4 py-3">难度</th><th class="px-4 py-3">共性次数</th>'
             f'<th class="px-4 py-3 text-left">学员</th><th class="px-4 py-3">AI 讲题</th></tr></thead>'
             f'<tbody>{rows_html}</tbody></table>',
        footer='<p class="text-center text-xs text-gray-400 mt-8">v3.5 Phase 3 · 教练批量视角 · 班级共性错题 + StudyMate AI 跳转</p>',
        STUDYMATE_BASE_URL=STUDYMATE_BASE_URL,
    )


# -- smoke test --
if __name__ == "__main__":
    print("[SMOKE] studymate_panel.py")

    # 1) 单学员面板
    panel = build_panel_for_student(570175, limit=5)
    print(f"  [OK] UID 570175 面板: {len(panel['items'])} 错题, "
          f"stats total={panel['stats']['total']}")
    if panel["items"]:
        it = panel["items"][0]
        print(f"  [OK] 例: {it['pid']} {it['title'][:20]}")
        print(f"        链接: {it['studymate_url'][:100]}...")
        assert "studymate.example.com/mistake" in it["studymate_url"]
        assert "token=" in it["studymate_url"]

    # 2) 学员档案缺失场景
    panel2 = build_panel_for_student(999999)
    assert panel2["warning"]
    print(f"  [OK] 学员档案缺失: warning 已提示")

    # 3) 班级批量
    batch = build_batch_panel(top_n=3)
    print(f"  [OK] 班级面板: {len(batch['per_student'])} 名学员, "
          f"共性错题 Top 3: {[c['pid'] for c in batch['common_mistakes'][:3]]}")

    # 4) HTML 渲染
    html = render_panel_html(panel, mode="student")
    assert "StudyMate AI 讲题面板" in html
    assert "AI 讲题" in html
    assert len(html) > 500
    print(f"  [OK] student 面板 HTML: {len(html)} 字符")

    html2 = render_panel_html(batch, mode="batch")
    assert "教练班级面板" in html2
    print(f"  [OK] batch 面板 HTML: {len(html2)} 字符")

    print("[OK] studymate_panel smoke test passed")
