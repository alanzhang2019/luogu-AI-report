import os
import json
import argparse
import math
import re
import hashlib
import time
import urllib.request
import zipfile
from pathlib import Path
from datetime import datetime
from typing import Any, Callable

from env_loader import load_dotenv

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from rich.console import Console
from rich.prompt import Prompt
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from openai import OpenAI
import pyLuogu
from pyLuogu.errors import AuthenticationError, ForbiddenError, RequestError
from examples.export_for_ai import (
    DETAIL_FETCH_SAMPLE_LIMIT_FAILED,
    DETAIL_FETCH_SAMPLE_LIMIT_PASSED,
    _build_tag_maps,
    _summarize,
    _pick_record_for_problem,
)

import markdown as md

load_dotenv(Path(__file__).resolve().parent / ".env")
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

console = Console()
DEFAULT_REPORT_MD = "luogu_coach_report.md"
DEFAULT_REPORT_HTML = "luogu_coach_report.html"
DEFAULT_REPORT_PDF = "luogu_coach_report.pdf"
DEFAULT_ASSETS_DIR = "luogu_report_assets"

DIAGNOSTIC_FRAMEWORK = """
【能力评估参考框架】（请对照此框架对用户进行诊断和分级建议）：
1. S级 - 计数与组合推导：赛时容易先写DFS/枚举，缺乏“统计对象集合”思维。需强化：组合数/容斥/DP/生成函数。
2. S级 - 图论建模与最短路变形：模板能写但建图边含义不稳，差分约束/分层图易卡。需强化：图的语义定义、最短路树。
3. A级 - 数据结构维护不变量：基础线段树能做，多标记易WA。需强化：节点信息明确数学定义、merge/pushdown的代数正确性。
4. A级 - DP 状态设计与优化：常规DP能写，维度多易爆复杂度。需强化：树形/区间/状压DP，单调队列优化。
5. A级 - 部分分升级能力：赛时能拿部分分，但不会倒推。需强化：从小n、小值域、树退化等子任务倒推正解。
6. B级 - 高级字符串结构：KMP/Hash有基础，自动机/SAM不稳定。需强化：节点代表的集合、Fail树/link的含义。
7. B级 - 计算几何：缺模板，少边界意识。需强化：向量/叉积、凸包、扫描线基础与eps处理。
8. B级 - 网络流/匹配：缺乏模式识别。需强化：建图谱系、最小割模型、费用流。
9. S级 - 复盘与错因沉淀：盲目改代码AC后就过。需强化：四段式复盘（赛时模型、错因、正解性质、代码不变量）。
"""


def find_chinese_font_path() -> str | None:
    def _try_download_lxgw_wenkai(dest_dir: Path) -> str | None:
        auto = os.environ.get("LUOGU_REPORT_AUTO_FONT_DOWNLOAD", "").strip().lower() in {"1", "true", "yes", "on"}
        if not auto:
            return None

        dest_dir.mkdir(parents=True, exist_ok=True)
        version = "1.520"
        zip_name = f"lxgw-wenkai-v{version}.zip"
        url = f"https://github.com/lxgw/LxgwWenKai/releases/download/v{version}/{zip_name}"
        expected_sha256 = "3a763543bec896e3c1badc9808bc804116a5e3d26f9f9592dacc834c9e799d8c"
        zip_path = dest_dir / zip_name
        extracted_font = dest_dir / "LXGWWenKai-Regular.ttf"

        if extracted_font.exists():
            return str(extracted_font)

        if zip_path.exists():
            try:
                h = hashlib.sha256()
                with open(zip_path, "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
                if h.hexdigest().lower() != expected_sha256:
                    zip_path.unlink(missing_ok=True)
            except Exception:
                try:
                    zip_path.unlink(missing_ok=True)
                except Exception:
                    pass

        if not zip_path.exists():
            tmp_path = dest_dir / (zip_name + ".tmp")
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "luogu-ai-report/1.0"})
                with urllib.request.urlopen(req, timeout=20) as resp, open(tmp_path, "wb") as out:
                    while True:
                        buf = resp.read(1024 * 1024)
                        if not buf:
                            break
                        out.write(buf)
                h = hashlib.sha256()
                with open(tmp_path, "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
                if h.hexdigest().lower() != expected_sha256:
                    tmp_path.unlink(missing_ok=True)
                    return None
                tmp_path.replace(zip_path)
            except Exception:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
                return None

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                member = f"lxgw-wenkai-v{version}/LXGWWenKai-Regular.ttf"
                if member not in zf.namelist():
                    return None
                tmp_extract = dest_dir / (extracted_font.name + ".tmp")
                with zf.open(member) as src, open(tmp_extract, "wb") as dst:
                    dst.write(src.read())
                tmp_extract.replace(extracted_font)
            return str(extracted_font) if extracted_font.exists() else None
        except Exception:
            return None

    env_font = os.environ.get("CHINESE_FONT_PATH") or os.environ.get("LUOGU_REPORT_FONT_PATH")
    if env_font and os.path.exists(env_font):
        return env_font

    local_candidates: list[str] = []
    try:
        base = Path(__file__).resolve().parent
        downloaded = _try_download_lxgw_wenkai(base / "assets" / "fonts")
        if downloaded and os.path.exists(downloaded):
            return downloaded
        local_candidates.extend(
            [
                str(base / "assets" / "fonts" / "NotoSansCJKsc-Regular.otf"),
                str(base / "assets" / "fonts" / "NotoSansSC-Regular.otf"),
                str(base / "assets" / "fonts" / "SourceHanSansCN-Regular.otf"),
                str(base / "assets" / "fonts" / "wqy-zenhei.ttc"),
                str(base / "assets" / "fonts" / "LXGWWenKai-Regular.ttf"),
            ]
        )
    except Exception:
        pass

    candidates = [
        *local_candidates,
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyh.ttf",
        r"C:\Windows\Fonts\msyhbd.ttf",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simkai.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/arphic/ukai.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    try:
        from matplotlib import font_manager

        preferred_families = [
            "Noto Sans CJK SC",
            "Noto Sans SC",
            "Source Han Sans CN",
            "WenQuanYi Zen Hei",
            "WenQuanYi Micro Hei",
            "Microsoft YaHei",
            "SimHei",
            "PingFang SC",
            "Arial Unicode MS",
        ]
        for family in preferred_families:
            try:
                fp = font_manager.FontProperties(family=family)
                font_file = font_manager.findfont(fp, fallback_to_default=False)
                if font_file and os.path.exists(font_file):
                    return font_file
            except Exception:
                continue
    except Exception:
        pass
    try:
        from matplotlib import font_manager

        keywords = (
            "notosanscjk",
            "notosanssc",
            "sourcehansans",
            "noto sans cjk",
            "noto sans sc",
            "wqy",
            "wenquanyi",
            "droidsansfallback",
            "arphic",
            "ukai",
            "uming",
            "simhei",
            "msyh",
            "yahei",
            "pingfang",
        )
        for font_path in font_manager.findSystemFonts(fontpaths=None, fontext="ttf") + font_manager.findSystemFonts(fontpaths=None, fontext="ttc") + font_manager.findSystemFonts(fontpaths=None, fontext="otf"):
            lower = font_path.lower()
            if any(k in lower for k in keywords) and os.path.exists(font_path):
                return font_path
    except Exception:
        pass
    return None


def configure_matplotlib_font() -> str | None:
    font_path = find_chinese_font_path()
    family_fallback = [
        "Noto Sans CJK SC",
        "Noto Sans SC",
        "Source Han Sans CN",
        "WenQuanYi Zen Hei",
        "WenQuanYi Micro Hei",
        "Microsoft YaHei",
        "SimHei",
        "PingFang SC",
        "Arial Unicode MS",
        "DejaVu Sans",
    ]
    if font_path:
        try:
            from matplotlib import font_manager

            font_manager.fontManager.addfont(font_path)
            font_name = font_manager.FontProperties(fname=font_path).get_name()
            plt.rcParams["font.sans-serif"] = [font_name, *family_fallback]
        except Exception:
            plt.rcParams["font.sans-serif"] = family_fallback
    else:
        plt.rcParams["font.sans-serif"] = family_fallback
    plt.rcParams["font.family"] = "sans-serif"
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["font.size"] = 12
    plt.rcParams["axes.titlesize"] = 14
    plt.rcParams["axes.labelsize"] = 12
    plt.rcParams["xtick.labelsize"] = 11
    plt.rcParams["ytick.labelsize"] = 11
    return font_path


def register_pdf_font() -> str:
    font_path = find_chinese_font_path()
    if font_path:
        try:
            pdfmetrics.registerFont(TTFont("CoachChinese", font_path))
            return "CoachChinese"
        except Exception:
            pass
    return "Helvetica"


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


DIFFICULTY_NAME_MAP = {
    0: "暂无评定",
    1: "入门",
    2: "普及-",
    3: "普及/提高-",
    4: "普及+/提高",
    5: "提高+/省选-",
    6: "省选/NOI-",
    7: "NOI/NOI+/CTSC",
}

DIFFICULTY_COLOR_MAP = {
    0: "#9CA3AF",
    1: "#FE4C61",
    2: "#F39C12",
    3: "#FFC116",
    4: "#52C41A",
    5: "#3498DB",
    6: "#9D4EDD",
    7: "#0E1D69",
}

DIFFICULTY_TEXT_COLOR_MAP = {
    0: "#111827",
    1: "#FFFFFF",
    2: "#111827",
    3: "#111827",
    4: "#FFFFFF",
    5: "#FFFFFF",
    6: "#FFFFFF",
    7: "#FFFFFF",
}

TAG_CHART_PALETTE = [
    "#52C41A",
    "#3498DB",
    "#9D4EDD",
    "#FE4C61",
    "#F39C12",
    "#14B8A6",
    "#FFC116",
    "#0EA5E9",
]


def _render_progress_bar(percentage: float, color: str, width_px: int = 150) -> str:
    pct = max(0.0, min(100.0, float(percentage)))
    return (
        f'<span style="display:inline-block;width:{width_px}px;height:12px;'
        'background:#E5E7EB;border-radius:9999px;overflow:hidden;vertical-align:middle;">'
        f'<span style="display:block;width:{pct:.1f}%;height:12px;background:{color};"></span>'
        "</span>"
    )


def get_difficulty_style(level: int) -> tuple[str, str, str]:
    return (
        DIFFICULTY_NAME_MAP.get(level, str(level)),
        DIFFICULTY_COLOR_MAP.get(level, "#4B5563"),
        DIFFICULTY_TEXT_COLOR_MAP.get(level, "#FFFFFF"),
    )


def summarize_average_difficulty(difficulty_histogram: dict) -> dict[str, str | int | float]:
    total = 0
    weighted = 0
    for key, value in difficulty_histogram.items():
        if str(key).isdigit():
            level = int(key)
            if level <= 0:
                continue
            total += int(value)
            weighted += level * int(value)

    average_value = weighted / total if total else 0.0
    candidate_levels = [k for k in DIFFICULTY_NAME_MAP.keys() if int(k) > 0]
    nearest_level = min(candidate_levels, key=lambda level: abs(level - average_value)) if total and candidate_levels else 0
    label, color, text_color = get_difficulty_style(nearest_level)
    return {
        "average_value": average_value,
        "nearest_level": nearest_level,
        "label": label,
        "color": color,
        "text_color": text_color,
    }


def render_star_rating_html(stars: str) -> str:
    filled_count = stars.count("⭐")
    empty_count = stars.count("☆")
    total_count = filled_count + empty_count
    if total_count == 0 or total_count > 5:
        return stars

    star_items = []
    for ch in stars:
        if ch == "⭐":
            star_items.append('<span style="color:#F5C542;text-shadow:0 1px 0 rgba(0,0,0,0.18);">★</span>')
        elif ch == "☆":
            star_items.append('<span style="color:#94A3B8;">★</span>')
        else:
            star_items.append(ch)

    return (
        '<span style="display:inline-flex;align-items:center;gap:2px;'
        'padding:2px 8px;border-radius:9999px;background:#111827;'
        'border:1px solid #374151;box-shadow:inset 0 1px 0 rgba(255,255,255,0.06);'
        'font-size:1.02em;line-height:1.1;vertical-align:middle;">'
        + "".join(star_items)
        + f'<span style="margin-left:6px;color:#CBD5E1;font-size:12px;font-weight:700;">{filled_count}/{total_count}</span>'
        "</span>"
    )


def split_practice_problems(practice) -> tuple[list[pyLuogu.ProblemSummary], list[pyLuogu.ProblemSummary]]:
    practice_problems = list(getattr(practice, "problems", []) or [])
    if practice_problems:
        passed = [p for p in practice_problems if getattr(p, "accepted", False)]
        failed = [p for p in practice_problems if getattr(p, "submitted", False) and not getattr(p, "accepted", False)]
        if passed or failed:
            return passed, failed

    raw = practice.data if isinstance(getattr(practice, "data", None), dict) else None
    passed: list[pyLuogu.ProblemSummary] = []
    failed: list[pyLuogu.ProblemSummary] = []
    passed_ids: set[str] = set()

    for key, target, accepted in (("passed", passed, True), ("submitted", failed, False), ("failed", failed, False)):
        items = raw.get(key) if isinstance(raw, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            pid = item.get("pid")
            if not pid:
                continue
            pid = str(pid)
            if accepted:
                passed_ids.add(pid)
            elif pid in passed_ids:
                continue
            target.append(
                pyLuogu.ProblemSummary(
                    {
                        "pid": pid,
                        "title": item.get("title") or item.get("name") or "",
                        "difficulty": item.get("difficulty"),
                        "type": item.get("type"),
                        "submitted": True,
                        "accepted": accepted,
                        "tags": item.get("tags") or [],
                        "totalSubmit": item.get("totalSubmit"),
                        "totalAccepted": item.get("totalAccepted"),
                        "flag": item.get("flag"),
                        "fullScore": item.get("fullScore"),
                    }
                )
            )
    return passed, failed


def collect_record_dicts(items: list[dict]) -> list[dict]:
    records: list[dict] = []
    for item in items:
        record = item.get("record")
        if isinstance(record, dict) and record.get("submitTime"):
            records.append(record)
    return records


def summarize_detail_fetch_stats(
    passed_items: list[dict] | None,
    failed_items: list[dict] | None,
    detail_fetch_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    items = list(passed_items or []) + list(failed_items or [])
    stats = {
        "total_items": len(items),
        "source_code_success": 0,
        "summary_only": 0,
        "detail_requested": 0,
        "detail_skipped": 0,
        "detail_errors": 0,
        "pure_error_records": 0,
        "blocker_reason": "",
    }
    for item in items:
        record = item.get("record")
        if not isinstance(record, dict):
            continue
        if record.get("_detail_requested"):
            stats["detail_requested"] += 1
        if record.get("sourceCode"):
            stats["source_code_success"] += 1
            continue
        if record.get("submitTime"):
            stats["summary_only"] += 1
        if record.get("_detail_skipped"):
            stats["detail_skipped"] += 1
            if not stats["blocker_reason"]:
                stats["blocker_reason"] = str(record.get("_detail_skipped") or "")
        if record.get("_detail_error"):
            stats["detail_errors"] += 1
            if not stats["blocker_reason"]:
                stats["blocker_reason"] = str(record.get("_detail_error") or "")
        if record.get("error") and not record.get("submitTime"):
            stats["pure_error_records"] += 1
            if not stats["blocker_reason"]:
                stats["blocker_reason"] = str(record.get("error") or "")

    if isinstance(detail_fetch_state, dict) and detail_fetch_state.get("last_detail_error"):
        stats["blocker_reason"] = str(detail_fetch_state.get("last_detail_error") or stats["blocker_reason"])
    return stats


def build_detail_fetch_overview(detail_fetch_stats: dict | None) -> dict[str, Any]:
    stats = detail_fetch_stats or {}
    total_items = int(stats.get("total_items", 0))
    source_code_success = int(stats.get("source_code_success", 0))
    summary_only = int(stats.get("summary_only", 0))
    detail_skipped = int(stats.get("detail_skipped", 0))
    pure_error_records = int(stats.get("pure_error_records", 0))
    blocker_reason = str(stats.get("blocker_reason") or "")

    if total_items <= 0:
        status_label = "未抓取详情"
        status_bg = "#E5E7EB"
        status_fg = "#374151"
    elif pure_error_records > 0:
        status_label = "存在失败"
        status_bg = "#FEE2E2"
        status_fg = "#991B1B"
    elif detail_skipped > 0:
        status_label = "已触发止损"
        status_bg = "#FEF3C7"
        status_fg = "#92400E"
    elif source_code_success > 0:
        status_label = "抓取稳定"
        status_bg = "#DCFCE7"
        status_fg = "#166534"
    else:
        status_label = "仅摘要保底"
        status_bg = "#DBEAFE"
        status_fg = "#1D4ED8"

    return {
        "status_label": status_label,
        "status_bg": status_bg,
        "status_fg": status_fg,
        "source_code_success": source_code_success,
        "summary_only": summary_only,
        "detail_skipped": detail_skipped,
        "pure_error_records": pure_error_records,
        "blocker_reason": blocker_reason or "无",
    }


def describe_behavior_fetch_error(exc: Exception) -> str:
    if isinstance(exc, AuthenticationError):
        return "未登录或 Cookies 已失效，无法读取提交记录列表"
    if isinstance(exc, ForbiddenError):
        return f"无权访问提交记录列表：{exc}"
    if isinstance(exc, RequestError):
        if getattr(exc, "status_code", None) == 429:
            return "请求提交记录过于频繁，请稍后重试"
        return f"请求提交记录失败：{exc}"
    message = str(exc).strip()
    if message:
        return message
    return "未获取到有效提交记录"


def enrich_problem_tags(
    luogu: pyLuogu.luoguAPI,
    problems: list[pyLuogu.ProblemSummary],
    *,
    max_fetch: int | None = None,
    progress_callback: Callable[[int, int, int], None] | None = None,
) -> int:
    """
    为缺失 tags 的题目按需补全标签。
    优先使用 practice.problems 自带标签；只有为空时才走 problem_detail 兜底。
    返回本次成功补全的题目数量。

    progress_callback(fetched, enriched, total_missing) 在每道题处理完后调用，
    用于向前端实时反馈标签抓取进度；传 None 则不回调。
    """
    enriched = 0
    fetched = 0
    cache: dict[str, list[int]] = {}

    # 先一次性统计需要补全的题目总数，方便前端显示 "X/Y" 进度
    missing_indices = [
        i for i, p in enumerate(problems)
        if not list(getattr(p, "tags", []) or [])
    ]
    total_missing = len(missing_indices)
    if progress_callback is not None:
        try:
            progress_callback(0, 0, total_missing)
        except Exception:
            pass

    for idx, problem in enumerate(problems):
        existing_tags = list(getattr(problem, "tags", []) or [])
        if existing_tags:
            continue
        if max_fetch is not None and fetched >= max_fetch:
            break

        pid = str(getattr(problem, "pid", "") or "")
        if not pid:
            continue

        try:
            if pid not in cache:
                fetched += 1
                detail = luogu.get_problem(pid)
                problem_detail = getattr(detail, "problem", None)
                cache[pid] = list(getattr(problem_detail, "tags", []) or [])
            if cache[pid]:
                problem.tags = list(cache[pid])
                enriched += 1
        except Exception:
            continue

        if progress_callback is not None:
            try:
                progress_callback(fetched, enriched, total_missing)
            except Exception:
                pass

    return enriched


def fetch_behavior_analysis(luogu: pyLuogu.luoguAPI, uid: int, fallback_items: list[dict] | None = None) -> dict:
    from behavior_analyzer import analyze_submission_behavior

    raw_records: list[dict] = []
    last_error = None
    for page in range(1, 26):
        try:
            record_list = luogu.get_record_list(page=page, uid=uid, user=str(uid))
            page_records = getattr(record_list, "records", None) or getattr(record_list, "data", None) or []
            normalized_records = [
                rec.to_json() if hasattr(rec, "to_json") else rec
                for rec in page_records
            ]
        except Exception as e:
            last_error = describe_behavior_fetch_error(e)
            break

        if not normalized_records:
            break
        raw_records.extend(normalized_records)
        if len(normalized_records) < 20 or len(raw_records) >= 1000:
            break

    if raw_records:
        behavior = analyze_submission_behavior(raw_records)
        behavior["_source"] = "record_list"
        if last_error:
            behavior["_warning"] = last_error
        return behavior

    fallback_records = collect_record_dicts(fallback_items or [])
    if fallback_records:
        behavior = analyze_submission_behavior(fallback_records)
        behavior["_source"] = "record_detail_fallback"
        if last_error:
            behavior["_warning"] = last_error
        return behavior

    return {"error": last_error or "未获取到有效提交记录"}


def repair_behavior_analysis_from_items(export_data: dict) -> dict:
    behavior = export_data.get("behavior_analysis", {}) or {}
    if behavior and "error" not in behavior and behavior.get("personality_scores"):
        return behavior

    fallback_records = collect_record_dicts(
        list(export_data.get("passed_items", []) or []) + list(export_data.get("failed_items", []) or [])
    )
    if not fallback_records:
        return behavior or {"error": "未获取到有效提交记录"}

    from behavior_analyzer import analyze_submission_behavior

    repaired = analyze_submission_behavior(fallback_records)
    repaired["_source"] = "record_detail_fallback_repaired"
    if behavior.get("_warning"):
        repaired["_warning"] = str(behavior["_warning"])
    elif behavior.get("error"):
        repaired["_warning"] = str(behavior["error"])
    export_data["behavior_analysis"] = repaired
    return repaired


def build_trusted_data_summary_md(export_data: dict) -> str:
    student_info = export_data.get("student_info", {}) or {}
    eval_time = str(student_info.get("eval_time") or "")
    summary = export_data.get("summary", {}) or {}
    difficulty_histogram = summary.get("difficulty_histogram", {}) or {}
    level_experience = summary.get("level_experience", {}) or {}
    detail_fetch_stats = export_data.get("detail_fetch_stats", {}) or {}
    syllabus_eval = export_data.get("syllabus_evaluation", {}) or {}

    total = 0
    for level in range(1, 8):
        total += int(difficulty_histogram.get(str(level), difficulty_histogram.get(level, 0)))
    total = total or 1
    lines = [
        "## 数据校准与真实统计",
        f"- 报告生成时间：{eval_time or '未知'}",
    ]
    lines.extend([
        "",
        "### 难度分布（程序生成）",
        '<table><thead><tr><th>洛谷难度</th><th>题数</th><th>占比</th><th>分布图</th></tr></thead><tbody>',
    ])

    for level in range(1, 8):
        count = int(difficulty_histogram.get(str(level), difficulty_histogram.get(level, 0)))
        name = DIFFICULTY_NAME_MAP[level]
        color = DIFFICULTY_COLOR_MAP[level]
        pct = count * 100 / total
        badge = (
            f'<span style="display:inline-block;padding:2px 10px;border-radius:6px;'
            f'background:{color};color:#fff;font-weight:600;">{name}</span>'
        )
        lines.append(
            "<tr>"
            f"<td>{badge}</td>"
            f"<td>{count}</td>"
            f"<td>{pct:.1f}%</td>"
            f"<td>{_render_progress_bar(pct, color)} <span style=\"margin-left:8px;\">{pct:.1f}%</span></td>"
            "</tr>"
        )
    lines.extend([
        "</tbody></table>",
    ])
    lines.extend(
        [
            "",
            "### 知识点覆盖统计表（按算法标签）",
            '<table><thead><tr><th>级别</th><th>已覆盖/总数</th><th>覆盖率</th><th>详细情况</th></tr></thead><tbody>',
        ]
    )

    for key, label in (
        ("csp_j", "入门级（CSP-J）"),
        ("csp_s", "提高级（CSP-S）"),
        ("provincial", "省选级"),
        ("noi", "NOI级"),
    ):
        group = syllabus_eval.get(key, {}) or {}
        stats = group.get("stats", {}) or {}
        total_topics = int(stats.get("total", 0))
        covered = total_topics - int(stats.get("空白", 0))
        coverage = group.get("coverage", 0)
        green = int(stats.get("精通", 0))
        yellow = int(stats.get("熟练", 0))
        orange = int(stats.get("入门", 0))
        blue = int(stats.get("初窥", 0))
        red = int(stats.get("空白", 0))
        details = f"🟢{green}项 🟡{yellow}项 🟠{orange}项 🔵{blue}项 🔴{red}项"
        lines.append(f"<tr><td><strong>{label.split('（')[0].replace('级','')}</strong></td><td>{covered}/{total_topics}</td><td>{coverage}%</td><td>{details}</td></tr>")

    lines.extend(
        [
            "</tbody></table>",
            "",
            "- 口径说明：本表只根据题目的算法标签评估知识点覆盖，表示“接触过”，不等于“熟练掌握”。",
        ]
    )

    # 知识树图谱（HTML 块，python-markdown 会原样保留到最终 HTML）
    lines.append("")
    lines.append("### 知识树图谱（按算法标签 · 掌握度可视化）")
    lines.append(build_knowledge_tree_html(syllabus_eval))

    return "\n".join(lines)


# 知识树中每个掌握度等级对应的视觉样式（背景色 / 边框 / 文字色）
_LEVEL_STYLES = {
    "精通": ("#166534", "#22C55E", "#DCFCE7"),  # 深绿字 / 绿色边 / 浅绿底
    "熟练": ("#854D0E", "#EAB308", "#FEF3C7"),  # 棕字 / 黄边 / 浅黄底
    "入门": ("#9A3412", "#F97316", "#FFEDD5"),  # 棕红字 / 橙边 / 浅橙底
    "初窥": ("#1E40AF", "#3B82F6", "#DBEAFE"),  # 深蓝字 / 蓝边 / 浅蓝底
    "空白": ("#9CA3AF", "#D1D5DB", "#F3F4F6"),  # 灰字 / 灰边 / 灰底
}


def _level_for_ac(ac_count: int) -> str:
    if ac_count >= 20:
        return "精通"
    if ac_count >= 10:
        return "熟练"
    if ac_count >= 3:
        return "入门"
    if ac_count >= 1:
        return "初窥"
    return "空白"


def build_knowledge_tree_html(syllabus_eval: dict) -> str:
    """
    渲染"知识树"HTML：
    - 4 大等级（CSP-J / CSP-S / 省选 / NOI）= 4 个分支
    - 每个知识点是一个胶囊/圆角标签，未点亮的（空白）置灰
    - 掌握度越高，背景越深；颜色为 5 档（绿/黄/橙/蓝/灰）
    - 顶部带图例，鼠标悬停可看 AC 数
    """
    group_keys = (
        ("csp_j", "CSP-J 入门", "🌱"),
        ("csp_s", "CSP-S 提高", "🌿"),
        ("provincial", "省选级", "🌳"),
        ("noi", "NOI 级", "🏆"),
    )

    legend = (
        '<div style="display:flex;flex-wrap:wrap;gap:10px;margin:6px 0 14px 0;'
        'font-size:12px;color:#374151;">'
        '<span style="font-weight:600;color:#1F2937;">图例：</span>'
        + "".join(
            f'<span style="display:inline-flex;align-items:center;gap:4px;">'
            f'<span style="display:inline-block;width:14px;height:14px;border-radius:4px;'
            f'background:{bg};border:1px solid {bd};"></span>'
            f'<span style="color:{fg};">{name}</span></span>'
            for name, (fg, bd, bg) in _LEVEL_STYLES.items()
        )
        + '<span style="color:#6B7280;">（颜色越深 = AC 数越多 = 掌握越好；灰白 = 完全未接触）</span>'
        "</div>"
    )

    branches_html: list[str] = []
    for key, title, icon in group_keys:
        group = syllabus_eval.get(key, {}) or {}
        details = group.get("details", []) or []
        stats = group.get("stats", {}) or {}
        coverage = group.get("coverage", 0)
        total = int(stats.get("total", 0))
        red = int(stats.get("空白", 0))

        topic_chips: list[str] = []
        for item in details:
            topic = str(item.get("topic", ""))
            ac = int(item.get("ac_count", 0) or 0)
            level = _level_for_ac(ac)
            fg, bd, bg = _LEVEL_STYLES[level]
            # 空白项 40% 透明度，看起来"未点亮"
            opacity = "0.45" if level == "空白" else "1"
            # 边框粗细：接触的更显眼
            border_w = "1px" if level == "空白" else ("2px" if level in ("初窥", "入门") else "2.5px")
            topic_chips.append(
                f'<span title="{topic} · AC {ac} · {level}" '
                f'style="display:inline-flex;align-items:center;gap:4px;'
                f'padding:4px 9px;border-radius:9999px;'
                f'background:{bg};border:{border_w} solid {bd};'
                f'color:{fg};font-size:12.5px;font-weight:600;'
                f'opacity:{opacity};cursor:default;'
                f'box-shadow:0 1px 2px rgba(0,0,0,0.04);">'
                f'<span>{topic}</span>'
                f'<span style="opacity:0.75;font-size:11px;font-weight:500;">{ac}</span>'
                f'</span>'
            )

        # 分支根节点 + 子节点列表
        branches_html.append(
            f'<div style="border:1px solid #E5E7EB;border-radius:14px;'
            f'padding:14px 14px 12px 14px;background:#FFFFFF;'
            f'box-shadow:0 1px 3px rgba(0,0,0,0.04);">'
            f'<div style="display:flex;align-items:center;justify-content:space-between;'
            f'margin-bottom:10px;">'
            f'<div style="font-weight:700;font-size:15px;color:#111827;">'
            f'{icon} {title}</div>'
            f'<div style="font-size:12px;color:#6B7280;">'
            f'已点亮 <span style="color:#059669;font-weight:700;">{total - red}</span>'
            f' / {total}（{coverage}%）</div>'
            f'</div>'
            f'<div style="display:flex;flex-wrap:wrap;gap:6px;line-height:1.6;">'
            + "".join(topic_chips)
            + "</div></div>"
        )

    # 整体用 grid 排版（>=900px 时 2 列；>=1200px 时 4 列；否则 1 列）
    html = (
        '<div style="margin:8px 0 18px 0;">'
        + legend
        + '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));'
        'gap:14px;">'
        + "".join(branches_html)
        + "</div></div>"
    )
    return html


def normalize_report_markdown(report_md: str, export_data: dict) -> str:
    """对 AI 输出做最小必要的纠偏，锁定难度名称并修正明显错误表述。"""
    normalized = report_md

    normalized = re.sub(
        r"(?ms)^\s{0,3}#{2,6}\s*知识点覆盖统计表（按算法标签）\s*\n+.*?(?=^\s{0,3}#{2,6}\s|\Z)",
        "",
        normalized,
    )
    normalized = re.sub(
        r"(?ms)^\s{0,3}#{2,6}\s*知识点覆盖表（按算法标签统计）\s*\n+.*?(?=^\s{0,3}#{2,6}\s|\Z)",
        "",
        normalized,
    )
    normalized = re.sub(
        r"(?ms)^\s{0,3}#{2,6}\s*知识树[^\n]*\n+.*?(?=^\s{0,3}#{2,6}\s|\Z)",
        "",
        normalized,
    )

    for idx, name in DIFFICULTY_NAME_MAP.items():
        normalized = re.sub(rf"难度\s*{idx}\b", name, normalized)
        normalized = re.sub(rf"难度{idx}\b", name, normalized)

    eval_time = str((export_data.get("student_info", {}) or {}).get("eval_time") or "").strip()
    if not eval_time:
        eval_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    if eval_time:
        date_only = eval_time.split(" ")[0]
        normalized = re.sub(r"(诊断日期[：:]\s*)([^\n<]+)", rf"\1{date_only}", normalized)
        normalized = re.sub(r"(\*\*生成时间\*\*[:：]\s*)([^\n<]+)", rf"\1{eval_time}", normalized)
        normalized = re.sub(r"(\*\*报告生成时间\*\*[:：]\s*)([^\n<]+)", rf"\1{eval_time}", normalized)
        normalized = re.sub(r"((?<!\*)生成时间(?!\*)[：:]\s*)([^\n<]+)", rf"\1{eval_time}", normalized)
        normalized = re.sub(r"((?<!\*)报告生成时间(?!\*)[：:]\s*)([^\n<]+)", rf"\1{eval_time}", normalized)
        normalized = re.sub(r"(<strong>\s*生成时间\s*</strong>\s*[：:]\s*)([^<\n]+)", rf"\1{eval_time}", normalized, flags=re.I)
        normalized = re.sub(r"(<strong>\s*报告生成时间\s*</strong>\s*[：:]\s*)([^<\n]+)", rf"\1{eval_time}", normalized, flags=re.I)
        normalized = normalized.replace("2025年4月", eval_time)

    behavior = export_data.get("behavior_analysis", {}) or {}
    if behavior and "error" not in behavior:
        time_points = sum(int(v) for v in (behavior.get("time_slot_distribution", {}) or {}).values())
        if time_points > 0:
            normalized = re.sub(r"无时间戳数据[^。！!\n]*[。！!]?", "已获取真实提交时间戳数据，并完成时段分布统计。", normalized)
            normalized = normalized.replace("无法分析。根据大量AC的记录，推测训练是其日常生活的重要组成。", "已依据真实提交时间戳、活跃天数与时段分布完成分析。")

    normalized = normalized.replace("一发入魂率", "首次 AC 通过分布")
    normalized = normalized.replace("一发入魂", "首次 AC 通过")

    def _build_difficulty_chart_section_md() -> str:
        summary = export_data.get("summary", {}) or {}
        hist = summary.get("difficulty_histogram", {}) or {}
        solved = int(export_data.get("solved_count", 0))
        failed = int(export_data.get("failed_count", 0))
        total_attempted = solved + failed

        def _count(levels: list[int]) -> int:
            s = 0
            for lv in levels:
                s += int(hist.get(str(lv), hist.get(lv, 0)))
            return s

        z1 = _count([1, 2, 3])
        z2 = _count([4, 5])
        z3 = _count([6])
        z4 = _count([7])
        z_total = max(1, z1 + z2 + z3 + z4)

        def _pct(v: int) -> str:
            return f"{(v * 100 / z_total):.1f}%"

        avg_info = summarize_average_difficulty(hist)
        avg_label = str(avg_info.get("label") or "")

        lines = [
            "## 3. 难度分布与水平研判",
            "",
            "![](assets/difficulty_histogram.png)",
            "",
            "![](assets/status_ratio.png)",
            "",
            f"- 平均难度：{avg_label}（均值 {float(avg_info.get('average_value') or 0):.2f}）",
            f"- 题目覆盖区间：入门~普及/提高-(1-3) {z1} 题（{_pct(z1)}）；普及+/提高~提高+/省选-(4-5) {z2} 题（{_pct(z2)}）；省选/NOI-(6) {z3} 题（{_pct(z3)}）；NOI/NOI+/CTSC(7) {z4} 题（{_pct(z4)}）。",
        ]
        if total_attempted > 0:
            lines.append(f"- 通过/未通过：已通过 {solved} 题，未通过 {failed} 题（总尝试 {total_attempted}）。")
        lines.append("")
        lines.append("结论：以难度分布与通过比例为准，当前训练重心应优先覆盖 4-6 档的典型模型题，避免只在 1-3 档堆题量。")
        return "\n".join(lines)

    # 用“图表 + 程序生成说明”替换 AI 的 ASCII 条形图段落，避免乱码/难读
    normalized = re.sub(
        r"(?ms)^\s{0,3}#{2,6}\s*3\.\s*难度分布与水平研判\s*\n+.*?(?=^\s{0,3}#{2,6}\s|\Z)",
        _build_difficulty_chart_section_md() + "\n\n",
        normalized,
    )

    trusted_block = build_trusted_data_summary_md(export_data)
    heading_match = re.match(r"^(# .+\n+)", normalized)
    if heading_match:
        head = heading_match.group(1)
        tail = normalized[len(head):]
        return f"{head}{trusted_block}\n\n{tail}"
    return f"{trusted_block}\n\n{normalized}"


def compute_ability_scores(export_data: dict) -> dict[str, int]:
    summary = export_data.get("summary", {}) or {}
    top_tags = summary.get("top_algorithm_tags", []) or summary.get("top_tags", []) or []
    difficulty_histogram = summary.get("difficulty_histogram", {}) or {}
    solved_count = int(export_data.get("solved_count", 0))
    failed_count = int(export_data.get("failed_count", 0))

    keyword_map = {
        "基础实现": [],
        "搜索 / DFS": ["dfs", "搜索", "回溯", "枚举", "树遍历"],
        "动态规划": ["dp", "背包", "区间", "树形", "状压"],
        "图论": ["图", "tarjan", "lca", "最短路", "并查集", "网络流", "匹配", "树"],
        "数据结构": ["线段树", "树状数组", "bit", "堆", "单调", "平衡树", "st表", "数据结构"],
        "字符串 / 数学": ["字符串", "kmp", "hash", "trie", "sam", "数论", "数学", "组合", "计数", "贪心", "构造", "证明"],
    }

    difficulty_total = 0
    weighted = 0
    for key, value in difficulty_histogram.items():
        if str(key).isdigit():
            difficulty_total += int(value)
            weighted += int(key) * int(value)
    avg_difficulty = weighted / difficulty_total if difficulty_total else 0

    scores: dict[str, int] = {}
    for ability, keywords in keyword_map.items():
        score = 35 + min(20, solved_count * 2) - min(12, failed_count * 2)
        if ability == "基础实现":
            score = 48 + min(28, solved_count * 2) + int(avg_difficulty * 4)
        for item in top_tags:
            tag_name = str(item.get("name") or "").lower()
            count = int(item.get("count", 0))
            if any(keyword in tag_name for keyword in keywords):
                score += min(18, count * 2)
        if ability in {"动态规划", "图论", "数据结构", "字符串 / 数学"}:
            score += int(avg_difficulty * 3)
        scores[ability] = max(20, min(95, int(score)))
    return scores


def generate_chart_images(export_data: dict, output_dir: str) -> dict[str, str]:
    ensure_dir(output_dir)
    plt.style.use("default")
    configure_matplotlib_font()
    repair_behavior_analysis_from_items(export_data)

    chart_paths: dict[str, str] = {}
    summary = export_data.get("summary", {}) or {}
    difficulty_histogram = summary.get("difficulty_histogram", {}) or {}
    top_tags = summary.get("top_algorithm_tags", []) or summary.get("top_tags", []) or []
    solved_count = int(export_data.get("solved_count", 0))
    failed_count = int(export_data.get("failed_count", 0))

    difficulty_meta = {level: get_difficulty_style(level) for level in DIFFICULTY_NAME_MAP}

    def _get_hist_count(key: int | str) -> int:
        if key in difficulty_histogram:
            return int(difficulty_histogram[key])
        skey = str(key)
        return int(difficulty_histogram.get(skey, 0))

    numeric_levels = []
    other_keys = []
    for k in difficulty_histogram.keys():
        ks = str(k)
        if ks.isdigit():
            level = int(ks)
            if level > 0:
                numeric_levels.append(level)
        else:
            other_keys.append(ks)

    numeric_levels = sorted(set(numeric_levels))
    other_keys = sorted(set(other_keys))

    if numeric_levels or other_keys:
        labels: list[str] = []
        values: list[int] = []
        colors: list[str] = []

        for level in numeric_levels:
            name, color, _ = difficulty_meta.get(level, (str(level), "#4C78A8", "#FFFFFF"))
            labels.append(name)
            values.append(_get_hist_count(level))
            colors.append(color)

        for k in other_keys:
            labels.append(k)
            values.append(_get_hist_count(k))
            colors.append("#4C78A8")

        fig, ax = plt.subplots(figsize=(8.6, 5.0), facecolor="#FFFFFF")
        x = list(range(len(labels)))
        bars = ax.bar(x, values, color=colors, width=0.68, edgecolor="none")
        ax.set_title("题目难度分布（按洛谷难度等级）")
        ax.set_xlabel("难度")
        ax.set_ylabel("题目数量")
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=20, ha="right", fontsize=12)
        ax.yaxis.grid(True, linestyle="--", linewidth=0.8, color="#E5E7EB")
        ax.set_axisbelow(True)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["left"].set_color("#CBD5E1")
        ax.spines["bottom"].set_color("#CBD5E1")
        max_value = max(values) if values else 0
        total_count = sum(values)
        for idx, (bar, value) in enumerate(zip(bars, values)):
            pct = (value / total_count * 100) if total_count else 0
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                value + max(max_value * 0.03, 0.12),
                f"{value} 题\n{pct:.1f}%",
                ha="center",
                va="bottom",
                fontsize=11,
                color=colors[idx],
                fontweight="bold",
            )
        fig.tight_layout()
        difficulty_path = os.path.join(output_dir, "difficulty_histogram.png")
        fig.savefig(difficulty_path, dpi=200, bbox_inches="tight", facecolor=fig.get_facecolor())
        plt.close(fig)
        chart_paths["difficulty"] = difficulty_path

    fig, ax = plt.subplots(figsize=(6.4, 4.4), facecolor="#FFFFFF")
    counts = [solved_count, failed_count]
    labels = ["已通过", "未通过"]
    colors_list = ["#52C41A", "#FE4C61"]
    if sum(counts) == 0:
        counts = [1]
        labels = ["暂无数据"]
        colors_list = ["#BAB0AC"]
    ax.pie(
        counts,
        labels=labels,
        autopct="%1.0f%%",
        startangle=90,
        colors=colors_list,
        wedgeprops={"width": 0.45, "edgecolor": "#FFFFFF"},
        textprops={"fontsize": 12},
    )
    ax.set_title("通过 / 未通过占比")
    fig.tight_layout()
    status_path = os.path.join(output_dir, "status_ratio.png")
    fig.savefig(status_path, dpi=200, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    chart_paths["status"] = status_path

    selected_tags = top_tags[:8]
    if selected_tags:
        fig, ax = plt.subplots(figsize=(8.4, 5.0), facecolor="#FFFFFF")
        tag_names = [str(item.get("name") or item.get("id")) for item in selected_tags][::-1]
        tag_counts = [int(item.get("count", 0)) for item in selected_tags][::-1]
        tag_colors = [TAG_CHART_PALETTE[idx % len(TAG_CHART_PALETTE)] for idx in range(len(tag_names))]
        bars = ax.barh(tag_names, tag_counts, color=tag_colors, edgecolor="none")
        ax.set_title("高频算法标签 Top 8")
        ax.set_xlabel("出现次数")
        ax.xaxis.grid(True, linestyle="--", linewidth=0.8, color="#E5E7EB")
        ax.set_axisbelow(True)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["left"].set_color("#CBD5E1")
        ax.spines["bottom"].set_color("#CBD5E1")
        for idx, (bar, value) in enumerate(zip(bars, tag_counts)):
            ax.text(value + 0.1, idx, str(value), va="center", fontsize=11, color=tag_colors[idx], fontweight="bold")
        fig.tight_layout()
        tags_path = os.path.join(output_dir, "top_tags.png")
        fig.savefig(tags_path, dpi=200, bbox_inches="tight", facecolor=fig.get_facecolor())
        plt.close(fig)
        chart_paths["tags"] = tags_path

    ability_scores = compute_ability_scores(export_data)
    radar_labels = list(ability_scores.keys())
    radar_values = [ability_scores[key] for key in radar_labels]
    if radar_labels:
        angles = [n / float(len(radar_labels)) * 2 * math.pi for n in range(len(radar_labels))]
        angles += angles[:1]
        radar_plot_values = radar_values + radar_values[:1]
        fig = plt.figure(figsize=(6.6, 6.2))
        ax = plt.subplot(111, polar=True)
        ax.set_theta_offset(math.pi / 2)
        ax.set_theta_direction(-1)
        ax.set_thetagrids([angle * 180 / math.pi for angle in angles[:-1]], radar_labels, fontsize=11)
        ax.set_ylim(0, 100)
        zone_colors = [
            (0, 40, "#FDECEC"),
            (40, 65, "#FFF3E0"),
            (65, 85, "#E8F4FF"),
            (85, 100, "#E7F6EC"),
        ]
        zone_angles = [n / 180.0 * math.pi for n in range(361)]
        for start, end, zone_color in zone_colors:
            ax.fill_between(zone_angles, start, end, color=zone_color, alpha=0.35)
        ax.plot(angles, radar_plot_values, color="#4C78A8", linewidth=2)
        ax.fill(angles, radar_plot_values, color="#4C78A8", alpha=0.25)
        ax.set_rgrids([20, 40, 60, 80, 100], angle=90, fontsize=10, color="#8A96A3")
        ax.set_title("能力雷达图", pad=18)
        radar_path = os.path.join(output_dir, "ability_radar.png")
        fig.savefig(radar_path, dpi=180, bbox_inches="tight")
        plt.close(fig)
        chart_paths["radar"] = radar_path
        
    # 生成性格画像雷达图
    behavior_data = export_data.get("behavior_analysis", {})
    personality_scores = behavior_data.get("personality_scores", {})
    if personality_scores:
        p_labels = list(personality_scores.keys())
        p_values = [personality_scores[k] for k in p_labels]
        angles = [n / float(len(p_labels)) * 2 * math.pi for n in range(len(p_labels))]
        angles += angles[:1]
        p_plot_values = p_values + p_values[:1]
        
        fig = plt.figure(figsize=(6.6, 6.2))
        ax = plt.subplot(111, polar=True)
        ax.set_theta_offset(math.pi / 2)
        ax.set_theta_direction(-1)
        ax.set_thetagrids([angle * 180 / math.pi for angle in angles[:-1]], p_labels, fontsize=12)
        ax.set_ylim(0, 100)
        
        # 性格雷达图配色使用偏橙色/活力的色调
        zone_colors = [
            (0, 40, "#F3F4F6"),
            (40, 60, "#E5E7EB"),
            (60, 80, "#FEF3C7"),
            (80, 100, "#FEF08A"),
        ]
        zone_angles = [n / 180.0 * math.pi for n in range(361)]
        for start, end, zone_color in zone_colors:
            ax.fill_between(zone_angles, start, end, color=zone_color, alpha=0.35)
            
        ax.plot(angles, p_plot_values, color="#D97706", linewidth=2.5)
        ax.fill(angles, p_plot_values, color="#F59E0B", alpha=0.3)
        ax.set_rgrids([20, 40, 60, 80, 100], angle=90, fontsize=10, color="#9CA3AF")
        ax.set_title("性格特质雷达图", pad=18, fontsize=12, fontweight="bold", color="#92400E")
        
        p_radar_path = os.path.join(output_dir, "personality_radar.png")
        fig.savefig(p_radar_path, dpi=180, bbox_inches="tight")
        plt.close(fig)
        chart_paths["personality_radar"] = p_radar_path

    # 生成首次 AC 提交次数分布柱状图
    ac_submit_distribution = behavior_data.get("ac_submit_distribution", {})
    if ac_submit_distribution:
        def _dist_get(mapping: dict, key: int) -> int:
            if key in mapping:
                return int(mapping[key])
            return int(mapping.get(str(key), 0))

        # 将字符串键转换为整数排序
        keys = []
        for k in ac_submit_distribution.keys():
            try:
                keys.append(int(k))
            except ValueError:
                pass
        keys.sort()
        
        # 准备 x 和 y 轴数据，合并 >= 10 的部分
        labels = []
        values = []
        count_10_plus = 0
        total_ac = sum(ac_submit_distribution.values())
        
        for k in keys:
            if k >= 10:
                count_10_plus += _dist_get(ac_submit_distribution, k)
            else:
                labels.append(str(k))
                values.append(_dist_get(ac_submit_distribution, k))
                
        if count_10_plus > 0:
            labels.append("10+")
            values.append(count_10_plus)

        if labels:
            fig, ax = plt.subplots(figsize=(8, 4.5))
            # 设置颜色：第一发是深蓝色，其他是浅蓝色
            colors = ["#2563EB" if l == "1" else "#93C5FD" for l in labels]
            bars = ax.bar(labels, values, color=colors, edgecolor="none")
            ax.set_title("首次 AC 提交次数分布", fontsize=12, fontweight="bold")
            ax.set_xlabel("AC 所需提交次数")
            ax.set_ylabel("题目数")
            
            # 在柱子上添加文字标签
            for bar, value in zip(bars, values):
                percentage = (value / total_ac * 100) if total_ac > 0 else 0
                height = bar.get_height()
                ax.text(bar.get_x() + bar.get_width()/2., height + 0.5,
                        f"{value}\n({percentage:.0f}%)",
                        ha="center", va="bottom", fontsize=10)
                        
            fig.tight_layout()
            ac_dist_path = os.path.join(output_dir, "ac_submit_distribution.png")
            fig.savefig(ac_dist_path, dpi=180, bbox_inches="tight")
            plt.close(fig)
            chart_paths["ac_submit_distribution"] = ac_dist_path

    return chart_paths


def build_html_and_pdf(
    report_md: str,
    export_data: dict,
    html_path: str,
    pdf_path: str,
    chart_paths: dict[str, str],
    export_pdf: bool = True,
) -> None:
    # 扩展 markdown，支持表格
    report_html = md.markdown(report_md, extensions=['tables', 'fenced_code'])
    report_html = re.sub(
        r"((?:⭐|☆){1,5})",
        lambda m: render_star_rating_html(m.group(1)),
        report_html,
    )
    
    # 替换错题分页
    # 在 6. **【未通过题目专属题解（从暴力到正解）】** 后面的 h3 题目标题前插入分页符
    report_html = re.sub(r'(<h3>Problem)', r'<div class="page-break"></div>\1', report_html)

    # 动态为表格中的“当前等级”和“优先级”添加圆角徽章颜色样式
    # 使用正则匹配 td 标签里的特定文字，加上 span 标签
    badge_style_base = "display:inline-block;padding:2px 8px;border-radius:9999px;border:1px solid;font-size:12px;font-weight:700;line-height:1.2;white-space:nowrap;"
    badge_styles = {
        "green": badge_style_base + "background:#DCFCE7;color:#166534;border-color:#86EFAC;",
        "orange": badge_style_base + "background:#FFEDD5;color:#9A3412;border-color:#FDBA74;",
        "red": badge_style_base + "background:#FEE2E2;color:#991B1B;border-color:#FCA5A5;",
        "gray": badge_style_base + "background:#F3F4F6;color:#374151;border-color:#D1D5DB;",
    }
    risk_legend_html = '<p style="margin:0 0 12px 0;color:#6b7280;font-size:13px;">优先级说明：S（高/立即处理） · A（中/近期处理） · B（低/可后置）。</p>'
    risk_legend_inserted = False

    level_rules = [
        (re.compile(r"(短板|明显短板|偏弱|弱|无涉及|未涉及|缺失|不会|没涉及|没有涉及|基础弱)", re.I), "red"),
        (re.compile(r"(中等偏稳|有基础|基础稳|待强化|会但赛时成本高|需要加强|高级弱|易错|不熟)", re.I), "orange"),
        (re.compile(r"(稳|强项|覆盖充分|中上|优秀|熟练|稳定)", re.I), "green"),
    ]

    def _clean_cell_inner(inner: str) -> str:
        inner = re.sub(r"</?p[^>]*>", "", inner, flags=re.I)
        inner = re.sub(r"<[^>]+>", "", inner)
        return inner.strip()

    def _wrap_td_inner(td_html: str, display_text: str, style_key: str) -> str:
        m = re.match(r"<td(?P<attrs>[^>]*)>(?P<inner>.*)</td>", td_html, flags=re.S | re.I)
        if not m:
            return td_html
        attrs = m.group("attrs") or ""
        return f'<td{attrs}><span style="{badge_styles[style_key]}">{display_text}</span></td>'

    def _process_table(table_html: str) -> str:
        nonlocal risk_legend_inserted
        is_ability_table = bool(
            re.search(r"<th[^>]*>\s*能力块\s*</th>", table_html, flags=re.I)
            and re.search(r"<th[^>]*>\s*当前等级\s*</th>", table_html, flags=re.I)
        )
        is_risk_table = bool(
            re.search(r"<th[^>]*>\s*优先级\s*</th>", table_html, flags=re.I)
            and re.search(r"<th[^>]*>\s*风险项\s*</th>", table_html, flags=re.I)
        )
        if not (is_ability_table or is_risk_table):
            return table_html

        def _row_repl(m: re.Match) -> str:
            row = m.group(0)
            if "<th" in row:
                return row
            tds = re.findall(r"<td[^>]*>.*?</td>", row, flags=re.S | re.I)
            if not tds:
                return row

            if is_ability_table:
                col_idx = 1
                if len(tds) <= col_idx:
                    return row
                target_td = tds[col_idx]
                inner = re.sub(r"^<td[^>]*>|</td>$", "", target_td, flags=re.S | re.I)
                text = _clean_cell_inner(inner)
                if not text:
                    return row
                style_key = None
                for rule, key in level_rules:
                    if rule.search(text):
                        style_key = key
                        break
                if not style_key:
                    return row
                new_td = _wrap_td_inner(target_td, text, style_key)
                return row.replace(target_td, new_td, 1)

            col_idx = 0
            if len(tds) <= col_idx:
                return row
            target_td = tds[col_idx]
            inner = re.sub(r"^<td[^>]*>|</td>$", "", target_td, flags=re.S | re.I)
            text = _clean_cell_inner(inner)
            normalized = (text or "").strip().upper()
            mapping = {
                "S": ("S（高/立即处理）", "red"),
                "A": ("A（中/近期处理）", "orange"),
                "B": ("B（低/可后置）", "green"),
            }
            if normalized not in mapping:
                return row
            label, style_key = mapping[normalized]
            new_td = _wrap_td_inner(target_td, label, style_key)
            return row.replace(target_td, new_td, 1)

        processed = re.sub(r"<tr>.*?</tr>", _row_repl, table_html, flags=re.S | re.I)
        if is_risk_table and not risk_legend_inserted:
            risk_legend_inserted = True
            return processed + risk_legend_html
        return processed

    report_html = re.sub(r"<table[^>]*>.*?</table>", lambda m: _process_table(m.group(0)), report_html, flags=re.S | re.I)

    # 准备模板数据
    summary = export_data.get("summary", {}) or {}
    difficulty_histogram = summary.get("difficulty_histogram", {}) or {}
    avg_difficulty_info = summarize_average_difficulty(difficulty_histogram)
    avg_difficulty = f"{float(avg_difficulty_info['average_value']):.1f}"
    detail_fetch_overview = build_detail_fetch_overview(export_data.get("detail_fetch_stats", {}) or {})
    
    top_tag = "暂无"
    top_tags = summary.get("top_algorithm_tags", []) or summary.get("top_tags", []) or []
    if top_tags:
        top_tag = str(top_tags[0].get("name") or top_tags[0].get("id"))

    # 渲染 HTML
    env = Environment(loader=FileSystemLoader('.'))
    template = env.get_template('report_template.html')
    html_dir = Path(html_path).resolve().parent
    
    def _chart_src(value: str) -> str:
        if not value:
            return ""
        if value.startswith("data:"):
            return value
        if value.startswith("file:///") or value.startswith("http://") or value.startswith("https://"):
            return value
        p = Path(value)
        if not p.exists():
            return value
        resolved = p.resolve()
        try:
            relative = resolved.relative_to(html_dir)
            return relative.as_posix()
        except ValueError:
            try:
                return resolved.relative_to(html_dir.parent).as_posix()
            except ValueError:
                return resolved.as_uri()

    chart_srcs = {k: _chart_src(v) for k, v in chart_paths.items()}

    rendered_html = template.render(
        export_data=export_data,
        report_html=report_html,
        chart_paths=chart_srcs,
        avg_difficulty=avg_difficulty,
        avg_difficulty_label=str(avg_difficulty_info["label"]),
        avg_difficulty_color=str(avg_difficulty_info["color"]),
        avg_difficulty_text_color=str(avg_difficulty_info["text_color"]),
        detail_fetch_overview=detail_fetch_overview,
        top_tag=top_tag
    )

    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(rendered_html)

    if not export_pdf:
        return

    # 导出为 PDF
    console.print("[cyan]正在调用 Playwright 将 HTML 导出为高质量 PDF...[/cyan]")
    temp_pdf_path = f"{pdf_path}.tmp"
    try:
        if os.path.exists(temp_pdf_path):
            os.remove(temp_pdf_path)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            # 加上 file:// 协议访问本地 HTML
            file_url = f"file:///{os.path.abspath(html_path).replace(os.sep, '/')}"
            page.goto(file_url)
            page.wait_for_load_state("networkidle")
            page.pdf(
                path=temp_pdf_path,
                format="A4",
                print_background=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"}
            )
            browser.close()
        os.replace(temp_pdf_path, pdf_path)
    except Exception as e:
        if os.path.exists(temp_pdf_path):
            try:
                os.remove(temp_pdf_path)
            except OSError:
                pass
        console.print(f"[red]PDF 导出失败（Playwright 错误），请确保已运行 `playwright install chromium`。\n错误详情：{e}[/red]")

def load_or_prompt_openai_config():
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_ADMIN_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL")
    
    if not key:
        console.print(Panel("[yellow]OpenAI API Key not found.[/yellow]\nThis tool requires an OpenAI-compatible API key to evaluate your code and generate suggestions.\nIt supports any third-party platform that provides OpenAI-compatible endpoints (e.g., DeepSeek, Moonshot, SiliconFlow, etc.).", title="Configuration"))
        key = Prompt.ask("Please enter your API Key")
        os.environ["OPENAI_API_KEY"] = key.strip()
        
    if not base_url:
        base_url_input = Prompt.ask("Please enter the API Base URL (leave blank for default OpenAI: https://api.openai.com/v1)")
        if base_url_input.strip():
            os.environ["OPENAI_BASE_URL"] = base_url_input.strip()
            base_url = base_url_input.strip()
            
    # Also ask for model if base URL is provided since different platforms have different model names
    model_name = os.environ.get("OPENAI_MODEL_NAME")
    if not model_name:
        default_model = "gpt-4o" if not base_url else ""
        model_input = Prompt.ask(f"Please enter the model name to use (leave blank for default: {default_model})")
        if model_input.strip():
            os.environ["OPENAI_MODEL_NAME"] = model_input.strip()
        else:
            os.environ["OPENAI_MODEL_NAME"] = default_model
            
    return key, base_url, os.environ.get("OPENAI_MODEL_NAME")

def load_or_prompt_cookies():
    cookie_file = Path("cookies.json")
    if cookie_file.exists():
        try:
            return pyLuogu.LuoguCookies.from_file(str(cookie_file))
        except Exception as e:
            console.print(f"[red]Failed to load cookies.json: {e}[/red]")
            
    console.print(Panel("[yellow]Luogu Cookies not found.[/yellow]\nTo fetch your submissions, we need your Luogu cookies.", title="Configuration"))
    client_id = Prompt.ask("Enter your __client_id cookie value")
    uid = Prompt.ask("Enter your _uid cookie value")
    
    cookies = pyLuogu.LuoguCookies({
        "__client_id": client_id.strip(),
        "_uid": uid.strip()
    })
    
    with open("cookies.json", "w", encoding="utf-8") as f:
        json.dump(cookies.to_json(), f, indent=2)
        
    return cookies

def _trim_to_safe_boundary(text: str | None) -> str:
    """把已生成的 partial 文本修剪到最后一个完整行，避免把半句话喂给模型续写。"""
    if not text:
        return ""
    text = text.rstrip()
    if not text:
        return ""
    # 优先尝试切到最后一个 "## " / "### " 之类的二级标题处，作为天然分段点
    boundary_candidates: list[int] = []
    for marker in ("\n## ", "\n### ", "\n#### "):
        idx = text.rfind(marker)
        if idx > 0:
            boundary_candidates.append(idx + 1)  # +1 保留换行符
    # 退化到最后一个换行
    last_newline = text.rfind("\n")
    if last_newline > 0:
        boundary_candidates.append(last_newline + 1)
    if not boundary_candidates:
        return text
    cut = max(boundary_candidates)
    # 至少要保留 80% 内容，否则保守地只切到最后一个换行
    if cut < int(len(text) * 0.2):
        return text
    return text[:cut].rstrip() + "\n"


def generate_ai_report(
    export_data: dict,
    api_key: str,
    base_url: str | None,
    model_name: str,
    *,
    output_path: str | None = None,
    resume_prefix: str | None = None,
) -> str:
    """生成 AI Markdown 报告。

    Args:
        export_data: 选手数据导出结构
        api_key: OpenAI 兼容 API Key
        base_url: 可选的第三方 Base URL
        model_name: 模型名
        output_path: 若提供，token 会以流式增量写入该文件，断连时 partial 留在文件里
        resume_prefix: 若提供，作为"已生成的开头"喂给模型，要求其直接续写
    """
    from syllabus_matcher import format_syllabus_report, load_syllabus_context

    repair_behavior_analysis_from_items(export_data)

    client_kwargs = {
        "api_key": api_key,
        "timeout": 1800.0,  # 30 分钟读超时，避免大报告被中途断开
    }
    if base_url:
        client_kwargs["base_url"] = base_url

    client = OpenAI(**client_kwargs)
    
    solved_count = export_data.get("solved_count", 0)
    failed_count = export_data.get("failed_count", 0)
    summary = export_data.get("summary", {})

    # 提取代码样本（通过的题）
    passed_samples = []
    for item in export_data.get("passed_items", []):
        record = item.get("record")
        if record and isinstance(record, dict) and record.get("sourceCode"):
            passed_samples.append(f"### Problem {item['problem']['pid']} - {item['problem']['title']} (Passed)\n```cpp\n{record['sourceCode'][:800]}\n```\n")
        if len(passed_samples) >= 3:
            break

    # 提取未通过/做错的题
    failed_samples = []
    for item in export_data.get("failed_items", []):
        record = item.get("record")
        pid = item['problem']['pid']
        title = item['problem']['title']
        code_str = ""
        if record and isinstance(record, dict) and record.get("sourceCode"):
            code_str = f"User's failed code snippet:\n```cpp\n{record['sourceCode'][:800]}\n```\n"
        failed_samples.append(f"### Problem {pid} - {title} (Attempted but NOT passed)\n{code_str}")
        if len(failed_samples) >= 5: # Limit failed examples
            break

    # 行为分析数据
    behavior_data = export_data.get("behavior_analysis", {})
    behavior_summary = ""
    if behavior_data and "error" not in behavior_data:
        from behavior_analyzer import format_behavior_summary
        behavior_summary = format_behavior_summary(behavior_data)
    else:
        behavior_summary = f"**提交行为分析**: {behavior_data.get('error', '未获取到提交记录数据。')}"

    # 代码风格静态分析
    from code_analyzer import analyze_code_style, format_code_analysis
    code_records = []
    for item in export_data.get("passed_items", []) + export_data.get("failed_items", []):
        if "record" in item and isinstance(item["record"], dict):
            code_records.append(item["record"])
    
    code_analysis_data = analyze_code_style(code_records)
    code_analysis_summary = format_code_analysis(code_analysis_data)

    # 大纲对标数据
    syllabus_eval = export_data.get("syllabus_evaluation", {})
    syllabus_summary = ""
    if syllabus_eval:
        syllabus_summary = format_syllabus_report(syllabus_eval)
    else:
        syllabus_summary = "**大纲知识点对标**: 未获取到评估数据。"

    # 六维评分
    six_dim = export_data.get("six_dimension_scores", {})
    six_dim_text = ""
    if six_dim:
        six_dim_text = "| 维度 | 评分 |\n|------|------|\n"
        for dim, score in six_dim.items():
            six_dim_text += f"| {dim} | {score} |\n"

    syllabus_context_info = load_syllabus_context(max_chars=20000)
    syllabus_context = ""
    if syllabus_context_info.get("content"):
        source_path = syllabus_context_info.get("path") or "未知路径"
        syllabus_context = (
            f"【2025 大纲真实来源】{syllabus_context_info.get('source')} | {source_path}\n"
            f"{syllabus_context_info['content']}\n\n"
        )

    import datetime
    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    difficulty_guide = """
洛谷难度映射请严格使用以下标准名称，不要写“难度1/难度2”：
- 0: 暂无评定（灰色）
- 1: 入门（红色）
- 2: 普及-（橙色）
- 3: 普及/提高-（黄色）
- 4: 普及+/提高（绿色）
- 5: 提高+/省选-（蓝色）
- 6: 省选/NOI-（紫色）
- 7: NOI/NOI+/CTSC（黑色）
"""

    prompt = f"""
你是一位顶级的算法竞赛金牌教练。我导出了一位选手的近期洛谷做题记录（包括已通过和尝试但未通过的题目代码）。
请你根据我提供的【能力评估参考框架】以及【官方考纲】，对他进行深度的诊断，并针对他【未做完/做错的题目】给出极具启发性的题解。

**报告生成时间**：{current_time}

{DIAGNOSTIC_FRAMEWORK}

{difficulty_guide}

{syllabus_context}

### 选手的全局数据统计
- 本次导出中已通过题数: {solved_count}
- 本次导出中未通过/卡住题数: {failed_count}
- 卡题数（定义：同一道题提交>=3次且最终未AC）: {len((behavior_data or {}).get('stuck_problems', [])) if isinstance(behavior_data, dict) else 0}
- 难度分布直方图: {json.dumps(summary.get('difficulty_histogram'))}
- 偏好的算法标签: {json.dumps(summary.get('top_algorithm_tags') or summary.get('top_tags'))}

### 六维能力评分
{six_dim_text if six_dim_text else '未计算'}

### 提交行为深度分析
{behavior_summary}

### 大纲知识点对标
{syllabus_summary}

{code_analysis_summary}

### 选手最近通过的代码样本（用于评估代码习惯）
{''.join(passed_samples) if passed_samples else '暂无代码'}

### 选手未做完/尝试失败的题目（重点出题解部分）
{''.join(failed_samples) if failed_samples else '暂无未通过的题目'}

请你输出一份结构化的 Markdown 辅导报告，必须包含以下部分。在生成 Markdown 时，请务必使用以下视觉元素增强表现力：
 - 评分请使用黄色星级，如 ⭐⭐⭐⭐☆ (使用 ⭐ 和 ☆)
 - 难度名称必须使用洛谷官方口径，如“入门 / 普及- / 普及+/提高 / 提高+/省选- / 省选/NOI-”，严禁写“难度1/难度2”
 - 不要生成黑白字符图表或黑白直方图；如果需要表达占比或难度，请优先使用 HTML 彩色徽章、彩色表格，或直接引用上方图表结论
 - 等级前缀符号请使用 🟢精通 | 🟡熟练 | 🟠入门 | 🔵初窥 | 🔴空白
 - 各处点评或结论段落，请使用 `<p class="text-blue-700 font-semibold">解读：...</p>` 样式包装。
 - 整个报告尽可能以 Markdown 表格、区块等图表化、直观的形式呈现，少用长篇大论的文字。

 1. **【选手概览与性格画像】**：
    基于提交行为数据，提炼选手的性格画像。**必须**用 Markdown 表格输出，表格列固定为：`| 性格维度 | 星级评分 | 拟人化评价 | 数据证据 |`。
    **必须包含 6 行**（顺序固定，不允许合并或省略任意一行）：
    1) 坚韧度  2) 完美主义  3) 冒险精神  4) 自律性  5) 调试耐心  6) 作息规律
    严禁把多行合并成一格（例如把"自律性"和"作息规律"合并为"自律性与规律性"），也严禁用列表/段落代替表格。
    星级使用 ⭐⭐⭐⭐⭐/⭐⭐⭐⭐☆/⭐⭐⭐☆☆/⭐⭐☆☆☆/⭐☆☆☆☆☆ 五档（与雷达图六个维度的口径一一对应）。
    每行数据证据栏必须引用具体数字（如提交时段、卡题次数、AC率、重交间隔等），不要写"数据不足"。

 2. **【提交行为深度分析】**：
    基于提供的提交行为数据，以表格和重点解读的形式，深入分析用户的提交习惯。必须包含以下子模块：
    - **死磕题目 TOP (提交次数最多)**：列出提交次数最多的几道题，分析原因。
     - **首次 AC 情况**：分析首次通过和多次尝试后通过的比例。
    - **其他显著行为特征**：如单日高强度刷题记录、长耗时题目等。
    (注意：此部分请用表格展示数据，并在表下附上 `<p class="text-blue-700 font-semibold">特征：...</p>`)

 3. **【难度分布与水平研判】**：
    分析选手的难度分布特征，判断其处于哪个阶段（入门/普及/提高/省选）。必须使用洛谷官方难度名称：暂无评定、入门、普及-、普及/提高-、普及+/提高、提高+/省选-、省选/NOI-、NOI/NOI+/CTSC。严禁输出“难度1/难度2/难度3”。

 4. **【六维能力雷达表与诊断】（评分参考：85-100 优秀 | 65-84 良好 | 40-64 基础 | <40 薄弱）**：
      输出 Markdown 表格，评估选手在六大维度的状态：`| 能力块 | 评分 | 当前等级 | 数据证据 | 已经具备 |`
      六大维度：基础算法、数据结构、图论、动态规划、字符串、数学。当前等级请使用前缀符号（如 🟢精通）。

  5. **【考纲精准定级与知识点盲区】**（根据提供的 NOI大纲 2025版）：
     - **当前对应等级水平**：明确指出该选手目前处于 CSP-J / CSP-S / 省选 / NOI 哪个阶段。
     - **知识点强弱项**：严格对照考纲中的知识点名词，列出其掌握得最好的 3 个考点，以及最薄弱的 3 个考点（使用 🟢🟡🔴 标注）。
     - **训练盲区**：指出他在当前等级中"完全没有涉及/刷题数据中缺失"的必考知识点。
     - **知识点覆盖与树状图**：不要再写知识点覆盖统计表或知识树（这些由程序自动生成，放在"数据校准与真实统计"小节）。你只需要在本节用 1-2 段话点评"哪些大分支（4 大等级）覆盖得好、哪些几乎为零，并给 1-2 条具体训练建议"即可。
     - **题目级别经历表**：单独说明做过多少道 CSP-S / 省选 / NOI 级别题，按来源标签与难度双证据解释，不要与知识点覆盖混为一谈。

  6. **【风险诊断与训练闭环表】**：
     输出 Markdown 表格：`| 优先级 | 风险项 | 触发场景 | 比赛症状 | 根因判断 | 训练专题 | 验收标准 |`
     - 行数至少 5 行，优先级使用 `S/A/B`。
     - 这个表必须是高度可执行的训练方案。

  7. **【代码质量与工程习惯深度分析】**：基于《源码静态风格分析》及代码样本，提供一份来自资深架构师视角的 Review。分析代码长度、宏定义习惯（如 `#define int long long`）、IO 优化、命名、STL 容器使用情况等。指出 2 个优点和 3 个必须改掉的坏习惯。

  8. **【定制训练题单（6个月路线图）】**：
     根据上述大纲盲区和薄弱项，定制一份分阶段的训练计划：
     - 第一阶段（Month 1-2）：巩固基础，补齐短板
     - 第二阶段（Month 3-4）：数据结构/算法突破
     - 第三阶段（Month 5-6）：提速与稳定
     每个阶段包含具体知识点 + 推荐题目（带洛谷题号）。

  9. **【核心建议（优先级排序）】**：
     列出 5-8 条核心建议，按优先级排序（🔴紧急 / 🟡重要 / 🟢建议）。例如：`🔴 紧急: 补加 ios::sync_with_stdio(false) 防止大数据 TLE`。

  10. **【未通过题目专属题解（从暴力到正解）】**：针对上面列出的"未做完/尝试失败的题目"，逐一出题解。
    - 绝不能直接给出最优解！
    - 必须严格遵循**"从暴力到正解的思考过程"**：
      a) **AI 题解摘要**：一句话点出这道题的核心思路或坑点。
      b) 暴力思路怎么想？（复杂度是多少，能拿多少部分分？）
      c) 瓶颈在哪里？（时间卡在哪，空间卡在哪？）
      d) 关键性质/不变量观察（Key Observation）。
      e) 最终正解的推导与核心代码结构。
      f) **推荐同类题**：推荐 1-2 道涉及相同考点或技巧的洛谷题目（标明题号和简要推荐理由）。
 """

    # 续写模式：在 prompt 末尾追加"已有开头，直接续写"指令
    if resume_prefix:
        trimmed_prefix = _trim_to_safe_boundary(resume_prefix)
        if trimmed_prefix:
            prompt = prompt + f"""

---

### 续写模式（重要）
以下是**已经生成的开头**（可能因网络中断/超时而中止），请你**直接从该前缀的下一个字符开始续写剩余部分**：
- **不要重复输出已有内容**（前缀已包含的内容一律不要再写一遍）
- **不要写"以下是..."、"好的"、"我继续"等开场白或导语**
- 保持与已有内容**完全一致**的 Markdown 风格、章节顺序、视觉元素（星级、徽章等）
- 如果你认为已有内容已经基本完整，请**直接输出 `===REPORT_COMPLETE===`** 单独一行作为收尾

[已生成内容开始]
{trimmed_prefix}
[已生成内容结束]
"""

    system_prompt = (
        "你是顶级算法竞赛教练，极其擅长引导学生通过“暴力-观察-优化”的过程推导正解，"
        "且熟悉各种算法训练框架。"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    if output_path:
        # 流式生成：把 token 实时写盘，断连时 partial 会留在文件里
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        initial_content = _trim_to_safe_boundary(resume_prefix) if resume_prefix else ""
        collected_chunks: list[str] = []
        with open(output_path, "w", encoding="utf-8") as f:
            if initial_content:
                f.write(initial_content)
                f.flush()
            try:
                stream = client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    stream=True,
                    timeout=1800.0,
                )
                for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta.content:
                        piece = chunk.choices[0].delta.content
                        collected_chunks.append(piece)
                        f.write(piece)
                        f.flush()
            except Exception:
                # 不吞异常：让上层 retry 捕获，但 partial 已经在文件里
                raise
        # 流式成功后做一次归一化（替换 AI 编的 ASCII 表/难度名/日期等），再覆盖回文件
        full_raw = initial_content + "".join(collected_chunks)
        normalized = normalize_report_markdown(full_raw, export_data)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(normalized)
        return normalized

    # 非流式：保持旧行为，方便 CLI 单独跑测
    response = client.chat.completions.create(
        model=model_name, # 使用用户指定的模型
        messages=messages,
        timeout=1800.0,
    )
    content = response.choices[0].message.content or ""
    return normalize_report_markdown(content, export_data)

def extract_problems_from_practice(practice_data, key: str):
    problems = []
    if isinstance(practice_data, dict):
        items = practice_data.get(key)
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict): continue
                pid = item.get("pid")
                if pid:
                    problems.append(
                        pyLuogu.ProblemSummary({
                            "pid": str(pid),
                            "title": item.get("title") or item.get("name") or "",
                            "difficulty": item.get("difficulty"),
                            "type": item.get("type"),
                            "tags": item.get("tags") or [],
                        })
                    )
    return problems

def main():
    parser = argparse.ArgumentParser(description="Luogu AI Evaluator - Coach Edition")
    parser.add_argument("--max-passed", type=int, default=10, help="Number of passed problems to fetch")
    parser.add_argument("--max-failed", type=int, default=5, help="Number of failed/unsolved problems to fetch")
    parser.add_argument("--report-md", default=DEFAULT_REPORT_MD, help="Markdown report output path")
    parser.add_argument("--report-pdf", default=DEFAULT_REPORT_PDF, help="PDF report output path")
    parser.add_argument("--assets-dir", default=DEFAULT_ASSETS_DIR, help="Directory for generated chart assets")
    args = parser.parse_args()
    
    console.print(Panel.fit("[bold cyan]Welcome to the Luogu AI Evaluator (Coach Edition)[/bold cyan]\n[dim]Incorporating Advanced Diagnostic Framework & Step-by-Step Editorials[/dim]"))
    
    # 收集学生信息
    console.print("\n[bold]为了生成更正式的报告，请填写测评基础信息（直接回车可跳过）：[/bold]")
    student_name = Prompt.ask("姓名", default="未知选手")
    school = Prompt.ask("学校", default="未知学校")
    grade = Prompt.ask("年级", default="未知年级")
    
    api_key, base_url, model_name = load_or_prompt_openai_config()
    cookies = load_or_prompt_cookies()
    
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True) as progress:
        task = progress.add_task("[cyan]Connecting to Luogu API...", total=None)
        
        try:
            luogu = pyLuogu.luoguAPI(cookies=cookies)
            me = luogu.me()
            uid = int(me.uid)
            progress.update(task, description=f"[green]Connected as User ID: {uid}[/green]")
            
            tag_by_id, type_by_id = _build_tag_maps(luogu)
            practice = luogu.get_user_practice(uid)
            
            from behavior_analyzer import compute_six_dimension_scores
            from syllabus_matcher import evaluate_all_topics

            all_passed_problems, all_failed_problems = split_practice_problems(practice)
            progress.update(task, description="[cyan]Backfilling missing problem tags when needed...")
            enrich_problem_tags(luogu, all_passed_problems)
            all_passed_problems.sort(key=lambda p: (p.difficulty if p.difficulty is not None else 10, p.pid), reverse=True)
            passed_problems = all_passed_problems[:args.max_passed]
            all_failed_problems.sort(key=lambda p: (p.difficulty if p.difficulty is not None else 10, p.pid), reverse=True)
            failed_problems = all_failed_problems[:args.max_failed]
            
            progress.update(task, description=f"[cyan]Fetching submissions for {len(passed_problems)} passed and {len(failed_problems)} failed problems...")
            
            detail_fetch_state: dict[str, object] = {}
            passed_items = []
            for idx, problem in enumerate(passed_problems):
                try:
                    record = _pick_record_for_problem(
                        luogu=luogu,
                        uid=uid,
                        pid=problem.pid,
                        max_records_to_try=2,
                        require_source_code=idx < DETAIL_FETCH_SAMPLE_LIMIT_PASSED,
                        detail_fetch_state=detail_fetch_state,
                    )
                except Exception as e:
                    record = {"error": str(e)}
                passed_items.append({"problem": problem.to_json(), "record": record})
                
            failed_items = []
            for idx, problem in enumerate(failed_problems):
                try:
                    record = _pick_record_for_problem(
                        luogu=luogu,
                        uid=uid,
                        pid=problem.pid,
                        max_records_to_try=2,
                        require_source_code=idx < DETAIL_FETCH_SAMPLE_LIMIT_FAILED,
                        detail_fetch_state=detail_fetch_state,
                    )
                except Exception as e:
                    record = {"error": str(e)}
                failed_items.append({"problem": problem.to_json(), "record": record})

            progress.update(task, description="[cyan]Fetching recent submissions for behavior analysis...")
            behavior_analysis = fetch_behavior_analysis(luogu, uid, passed_items + failed_items)
            behavior_analysis = repair_behavior_analysis_from_items(
                {
                    "passed_items": passed_items,
                    "failed_items": failed_items,
                    "behavior_analysis": behavior_analysis,
                }
            )
            detail_fetch_stats = summarize_detail_fetch_stats(passed_items, failed_items, detail_fetch_state)

            summary = _summarize(all_passed_problems, tag_by_id=tag_by_id)
            syllabus_evaluation = evaluate_all_topics(summary.get("top_algorithm_tags", []) or summary.get("top_tags", []))
            six_dim_scores = compute_six_dimension_scores(
                {"solved_count": len(all_passed_problems), "summary": summary},
                behavior_analysis if "error" not in behavior_analysis else {},
            )
            
            import datetime
            export_data = {
                "student_info": {
                    "name": student_name,
                    "school": school,
                    "grade": grade,
                    "eval_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                },
                "solved_count": len(all_passed_problems),
                "failed_count": len(all_failed_problems),
                "summary": summary,
                "passed_items": passed_items,
                "failed_items": failed_items,
                "detail_fetch_stats": detail_fetch_stats,
                "behavior_analysis": behavior_analysis,
                "syllabus_evaluation": syllabus_evaluation,
                "six_dimension_scores": six_dim_scores,
            }
            
            progress.update(task, description=f"[cyan]Analyzing with {model_name} (Applying diagnostic framework & generating editorials)...")
            report_md = generate_ai_report(export_data, api_key, base_url, model_name)
            progress.update(task, description="[green]Analysis complete!")
            
        except Exception as e:
            console.print(f"[red]Error during execution: {e}[/red]")
            return

    console.print("\n")
    console.print(Panel(Markdown(report_md), title="[bold magenta]AI Evaluation & Coaching Report[/bold magenta]"))

    with open(args.report_md, "w", encoding="utf-8") as f:
        f.write(report_md)

    chart_paths = generate_chart_images(export_data, args.assets_dir)
    build_html_and_pdf(report_md, export_data, DEFAULT_REPORT_HTML, args.report_pdf, chart_paths)

    console.print(f"\n[green]Markdown 报告已保存到 {os.path.abspath(args.report_md)}[/green]")
    console.print(f"[green]HTML 报告已保存到 {os.path.abspath(DEFAULT_REPORT_HTML)}[/green]")
    console.print(f"[green]PDF 报告已保存到 {os.path.abspath(args.report_pdf)}[/green]")
    if chart_paths:
        console.print(f"[green]图表资源已保存到 {os.path.abspath(args.assets_dir)}[/green]")

if __name__ == "__main__":
    main()
