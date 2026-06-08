import os
import json
import uuid
import threading
import time
import hmac
from pathlib import Path
from datetime import datetime, date
from urllib.parse import urlsplit, urlunsplit
from openai import APIConnectionError, APITimeoutError, APIError, RateLimitError as OpenAIRateLimitError
try:
    from flask import Flask, render_template_string, request, redirect, url_for, send_from_directory, send_file, session, flash
except ImportError:
    from flask import Flask, render_template_string, request, redirect, url_for, send_from_directory, send_file, session, flash
    session = {}

from env_loader import load_dotenv

os.environ.setdefault("LUOGU_REPORT_AUTO_FONT_DOWNLOAD", "1")
_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env")

# ========== 本地默认 API 配置（请在这里填写） ==========
DEFAULT_API_KEY = ""
DEFAULT_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")                      # 例如: "https://api.openai.com/v1"
DEFAULT_MODEL_NAME = os.environ.get("OPENAI_MODEL_NAME", "gpt-4o-mini")              # 例如: "gpt-4o"
DEFAULT_ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
DEFAULT_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me-now")
# ======================================================


# ========== v3.5 Month 1 周末修复：安全基线检查 ==========
# 强制从 env 读取 admin 密码 + session secret；缺一即拒启动。
# 开发模式可设 ALLOW_INSECURE_DEFAULT=1 跳过检查。
INSECURE_DEFAULT_MARKERS = {
    "",
    "change-me-now",
    "luogu-ai-report-admin-secret-change-me",
    "secret",
    "flask-secret",
    "admin",
    "password",
}


def _check_security_baseline() -> None:
    """启动期硬性安全检查：未通过则 SystemExit(1)"""
    if os.environ.get("ALLOW_INSECURE_DEFAULT") == "1":
        print("[WARN] ALLOW_INSECURE_DEFAULT=1 已启用，跳过安全基线检查。生产环境务必禁用。")
        return

    issues: list[str] = []

    # 1) admin 密码：必须从 env 显式设置，且不能是已知弱默认
    admin_pwd = os.environ.get("ADMIN_PASSWORD", "")
    if not admin_pwd or admin_pwd.strip() in INSECURE_DEFAULT_MARKERS:
        issues.append(
            "ADMIN_PASSWORD 未设置或为已知弱默认（如 'change-me-now' / 'admin' / 'password'）"
        )

    # 2) Flask session secret：必须从 env 显式设置
    secret = (
        os.environ.get("ADMIN_SESSION_SECRET")
        or os.environ.get("FLASK_SECRET_KEY")
        or ""
    )
    if not secret.strip() or secret.strip() in INSECURE_DEFAULT_MARKERS:
        issues.append(
            "ADMIN_SESSION_SECRET / FLASK_SECRET_KEY 未设置或为已知弱默认"
        )

    if issues:
        print("=" * 64)
        print("[FATAL] 安全基线检查失败（v3.5 Month 1 周末修复）")
        for i, issue in enumerate(issues, 1):
            print(f"  {i}. {issue}")
        print()
        print("修复方式（在 .env 或环境变量中设置）：")
        print("  ADMIN_PASSWORD=<你的强密码>")
        print("  ADMIN_SESSION_SECRET=<32+ 字符随机串>")
        print()
        print("临时跳过（仅本地开发）：")
        print("  ALLOW_INSECURE_DEFAULT=1")
        print("=" * 64)
        raise SystemExit(1)


_check_security_baseline()

import pyLuogu
from examples.export_for_ai import (
    DETAIL_FETCH_SAMPLE_LIMIT_FAILED,
    DETAIL_FETCH_SAMPLE_LIMIT_PASSED,
    _build_tag_maps,
    _summarize,
    _pick_record_for_problem,
)
from pyLuogu.errors import AuthenticationError, ForbiddenError, RequestError
from luogu_evaluator import (
    generate_ai_report,
    generate_chart_images,
    build_html_and_pdf,
    DEFAULT_REPORT_MD,
    DEFAULT_REPORT_HTML,
    DEFAULT_REPORT_PDF,
    DEFAULT_ASSETS_DIR,
    split_practice_problems,
    fetch_behavior_analysis,
    repair_behavior_analysis_from_items,
    summarize_detail_fetch_stats,
    enrich_problem_tags,
)
from behavior_analyzer import (
    compute_six_dimension_scores,
    format_behavior_summary,
)
from syllabus_matcher import (
    evaluate_all_topics,
    format_syllabus_report,
    get_weak_topics,
    get_strong_topics,
)
from task_store import (
    insert_task,
    update_task,
    get_task,
    list_tasks,
    get_stats,
)

app = Flask(__name__)
app.secret_key = (
    os.environ.get("ADMIN_SESSION_SECRET")
    or os.environ.get("FLASK_SECRET_KEY")
    or "luogu-ai-report-admin-secret-change-me"
)

# 任务状态锁（数据库操作线程安全）
TASKS_LOCK = threading.Lock()
REBUILD_TASKS_LOCK = threading.Lock()
REBUILD_TASKS: dict[str, dict[str, str]] = {}
ACTIVE_GENERATION_TASKS_LOCK = threading.Lock()
ACTIVE_GENERATION_TASKS: dict[str, threading.Thread] = {}
AI_GENERATION_MAX_RETRIES = 4
AI_GENERATION_RETRY_SLEEP_SECONDS = 12


def get_admin_credentials() -> tuple[str, str]:
    return (
        str(os.environ.get("ADMIN_USERNAME", DEFAULT_ADMIN_USERNAME) or "").strip(),
        str(os.environ.get("ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD) or ""),
    )


def check_admin_credentials(username: str, password: str) -> bool:
    expected_username, expected_password = get_admin_credentials()
    username = str(username or "").strip()
    password = str(password or "")
    return (
        bool(expected_username)
        and bool(expected_password)
        and hmac.compare_digest(username, expected_username)
        and hmac.compare_digest(password, expected_password)
    )


def register_active_generation_task(task_id: str, thread: threading.Thread) -> None:
    with ACTIVE_GENERATION_TASKS_LOCK:
        ACTIVE_GENERATION_TASKS[task_id] = thread


def unregister_active_generation_task(task_id: str) -> None:
    with ACTIVE_GENERATION_TASKS_LOCK:
        ACTIVE_GENERATION_TASKS.pop(task_id, None)


def is_generation_task_active(task_id: str) -> bool:
    with ACTIVE_GENERATION_TASKS_LOCK:
        thread = ACTIVE_GENERATION_TASKS.get(task_id)
        if thread is None:
            return False
        if thread.is_alive():
            return True
        ACTIVE_GENERATION_TASKS.pop(task_id, None)
        return False


def get_active_generation_task_count() -> int:
    with ACTIVE_GENERATION_TASKS_LOCK:
        stale_ids = [task_id for task_id, thread in ACTIVE_GENERATION_TASKS.items() if not thread.is_alive()]
        for task_id in stale_ids:
            ACTIVE_GENERATION_TASKS.pop(task_id, None)
        return len(ACTIVE_GENERATION_TASKS)


def reconcile_stale_generation_tasks() -> int:
    stale_task_ids: list[str] = []
    for row in list_tasks():
        task_id = str(row.get("task_id", "") or "")
        status = str(row.get("status", "") or "")
        if status not in {"queued", "running"}:
            continue
        if is_generation_task_active(task_id):
            continue
        stale_task_ids.append(task_id)

    if not stale_task_ids:
        return 0

    with TASKS_LOCK:
        for task_id in stale_task_ids:
            update_task(
                task_id,
                status="error",
                stage="已中断",
                message="任务已中断：服务已重启或后台线程已退出，请重新生成。",
            )
    return len(stale_task_ids)


def sanitize_admin_next(next_url: str | None) -> str:
    value = str(next_url or "").strip()
    if value.startswith("/admin"):
        return value
    return "/admin"


def is_admin_authenticated() -> bool:
    expected_username, _ = get_admin_credentials()
    return (
        bool(expected_username)
        and bool(session.get("admin_authed"))
        and str(session.get("admin_user", "") or "") == expected_username
    )


def require_admin_auth():
    if is_admin_authenticated():
        return None
    return redirect(url_for("admin_login", next=sanitize_admin_next(getattr(request, "path", "/admin"))))


def set_rebuild_state(task_id: str, status: str, message: str = "") -> None:
    with REBUILD_TASKS_LOCK:
        REBUILD_TASKS[task_id] = {
            "status": str(status or ""),
            "message": str(message or ""),
        }


def clear_rebuild_state(task_id: str) -> None:
    with REBUILD_TASKS_LOCK:
        REBUILD_TASKS.pop(task_id, None)


def get_rebuild_state(task_id: str) -> dict[str, str]:
    with REBUILD_TASKS_LOCK:
        state = REBUILD_TASKS.get(task_id, {}) or {}
    return {
        "status": str(state.get("status", "") or ""),
        "message": str(state.get("message", "") or ""),
    }


def _source_cache_dir(uid: int | str) -> Path:
    return _ROOT / ".source_cache" / str(uid)


def _source_cache_file(uid: int | str, pid: str) -> Path:
    safe_pid = "".join(ch for ch in str(pid or "").strip() if ch.isalnum() or ch in {"_", "-"})
    return _source_cache_dir(uid) / f"{safe_pid or 'unknown'}.json"


def load_cached_source_record(uid: int | str, pid: str) -> dict | None:
    cache_file = _source_cache_file(uid, pid)
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict) and data.get("sourceCode"):
        return data
    return None


def save_cached_source_record(uid: int | str, pid: str, record: dict | None) -> None:
    if not isinstance(record, dict) or not record.get("sourceCode"):
        return
    cache_file = _source_cache_file(uid, pid)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(record)
    payload["_cached_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _parse_admin_time(value: str) -> datetime:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return datetime.min


def discover_orphan_report_tasks() -> list[dict]:
    report_root = _ROOT / "reports"
    if not report_root.exists():
        return []

    known_prefixes = {
        str(row.get("task_id", "") or "")[:8]
        for row in list_tasks()
    }
    discovered: list[dict] = []

    for report_dir in report_root.iterdir():
        if not report_dir.is_dir():
            continue
        folder_name = report_dir.name
        if folder_name.startswith("_"):
            continue

        folder_prefix = folder_name.split("_", 1)[0]
        if folder_prefix in known_prefixes:
            continue

        html_path = report_dir / "report.html"
        pdf_path = report_dir / "report.pdf"
        md_path = report_dir / "report.md"
        export_json_path = report_dir / "export_data.json"
        if not any(path.exists() for path in (html_path, pdf_path, md_path, export_json_path)):
            continue

        data = {}
        if export_json_path.exists():
            try:
                data = json.loads(export_json_path.read_text(encoding="utf-8"))
            except Exception:
                data = {}

        student_info = data.get("student_info", {}) if isinstance(data, dict) else {}
        solved_count = data.get("solved_count", "-") if isinstance(data, dict) else "-"
        failed_count = data.get("failed_count", "-") if isinstance(data, dict) else "-"
        name_from_dir = folder_name.split("_", 1)[1] if "_" in folder_name else folder_name
        name = str(student_info.get("name") or name_from_dir or "未知")
        school = str(student_info.get("school") or "未知学校")
        grade = str(student_info.get("grade") or "未知年级")
        eval_time = str(student_info.get("eval_time") or "")

        existing_files = [path for path in (html_path, pdf_path, md_path, export_json_path) if path.exists()]
        latest_time = max((path.stat().st_mtime for path in existing_files), default=report_dir.stat().st_mtime)
        display_time = eval_time or datetime.fromtimestamp(latest_time).strftime("%Y-%m-%d %H:%M")

        discovered.append({
            "id": folder_name,
            "name": name,
            "school": school,
            "grade": grade,
            "solved": solved_count,
            "failed": failed_count,
            "status": "done" if html_path.exists() or md_path.exists() or pdf_path.exists() else "unknown",
            "time": display_time,
            "html": _report_url(html_path) if html_path.exists() else "",
            "pdf": _download_report_url(_report_url(pdf_path)) if pdf_path.exists() else "",
            "md": _report_url(md_path) if md_path.exists() else "",
            "rebuild_status": "",
            "rebuild_message": "该报告目录未入库，仅支持查看与下载。",
            "can_rebuild": False,
            "is_orphan": True,
            "sort_time": _parse_admin_time(display_time),
        })

    return discovered


def describe_generation_error(exc: Exception, stage: str) -> str:
    stage_prefix = f"[阶段: {stage}] "
    message_lower = str(exc).lower()
    if isinstance(exc, ValueError):
        return stage_prefix + str(exc)
    if isinstance(exc, AuthenticationError):
        if stage == "预检提交记录权限" or stage == "抓取提交记录与代码":
            return stage_prefix + "Cookies 无效或已失效，无法读取提交记录，请重新获取同一会话下的 __client_id、_uid 和 C3VK。"
        if stage == "预检做题记录权限" or stage == "获取标签与练习数据":
            return stage_prefix + "Cookies 无效或已失效，无法读取练习数据，请重新获取 __client_id、_uid 和 C3VK。"
        if stage == "获取标签与练习数据":
            return stage_prefix + "Cookies 无效或已失效，无法读取练习数据，请重新获取 __client_id、_uid 和 C3VK。"
        return stage_prefix + "Cookies 无效或已失效，请重新登录洛谷并更新 Cookies。"
    if isinstance(exc, ForbiddenError):
        return stage_prefix + f"访问被拒绝：{exc}"
    if isinstance(exc, RequestError):
        return stage_prefix + str(exc)
    if _is_retryable_ai_error(exc) and stage == "生成 AI 报告":
        return stage_prefix + f"AI 接口连接失败：{exc}。可直接点“返回重试”，系统会自动回填参数；同时已加入自动重试，短时网络抖动会自行恢复。"
    if "missing credentials" in message_lower and stage == "生成 AI 报告":
        return stage_prefix + "未配置 OpenAI API Key：请在页面填写 OpenAI API Key，或在服务端设置环境变量 OPENAI_API_KEY / OPENAI_ADMIN_KEY，并重启服务使其生效。"
    base_detail = f"{type(exc).__name__}: {exc!r}"
    try:
        sc = getattr(exc, "status_code", None)
        if sc is not None:
            base_detail += f" [status_code={sc}]"
        body = getattr(exc, "body", None)
        if body is not None:
            base_detail += f" [body={str(body)[:300]}]"
        code = getattr(exc, "code", None)
        if code is not None:
            base_detail += f" [code={code}]"
    except Exception:
        pass
    print(f"[ERROR][{stage}] {base_detail}", flush=True)
    return stage_prefix + f"{base_detail}"


def resolve_openai_api_key(form: dict) -> tuple[str, str]:
    from_form = str(form.get("api_key", "") or "").strip()
    if from_form:
        return from_form, "form"
    for env_name in ("OPENAI_API_KEY", "OPENAI_ADMIN_KEY"):
        env_value = str(os.environ.get(env_name, "") or "").strip()
        if env_value:
            return env_value, f"env:{env_name}"
    return "", "missing"


def _is_retryable_ai_error(exc: Exception) -> bool:
    if isinstance(exc, (APIConnectionError, APITimeoutError, OpenAIRateLimitError)):
        return True
    if isinstance(exc, APIError):
        status_code = getattr(exc, "status_code", None)
        if status_code in {408, 409, 425, 429, 500, 502, 503, 504}:
            return True
    message = str(exc or "").strip().lower()
    retryable_keywords = (
        "connection error",
        "api connection error",
        "timed out",
        "timeout",
        "connection",
        "temporarily unavailable",
        "rate limit",
        "server error",
        "502",
        "503",
        "504",
    )
    return any(keyword in message for keyword in retryable_keywords)

INDEX_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>信竞 AI 报告 · 选手成长平台 · v3.5.2</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        .app-body{background:linear-gradient(135deg,#f0fdf4 0%,#ecfeff 100%);min-height:100vh;font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,"Noto Sans","PingFang SC","Microsoft YaHei",sans-serif;}
        .app-card{background:#fff;border-radius:16px;box-shadow:0 10px 25px rgba(0,0,0,.06);padding:32px;width:100%;}
        .app-title{font-size:26px;font-weight:800;color:#064e3b;margin:0 0 4px;letter-spacing:-0.5px;}
        .app-subtitle{color:#0f766e;margin:0 0 4px;font-size:14px;font-weight:600;}
        .app-tag{color:#6b7280;margin:0 0 18px;font-size:12px;}
        .app-muted{color:#9ca3af;font-size:12px;margin:0 0 18px;}
        .app-box{border-radius:10px;padding:12px 12px;border:1px solid #e5e7eb;}
        .app-box-yellow{background:#fffbeb;border-color:#fde68a;color:#92400e;}
        .app-box-blue{background:#eff6ff;border-color:#bfdbfe;color:#1d4ed8;}
        .app-box-green{background:#ecfdf5;border-color:#a7f3d0;color:#065f46;}
        .app-box-red{background:#fef2f2;border-color:#fecaca;color:#991b1b;}
        .app-label{display:block;font-size:13px;font-weight:700;color:#374151;}
        .app-input{margin-top:6px;display:block;width:100%;border-radius:10px;border:1px solid #d1d5db;padding:10px 12px;box-shadow:0 1px 2px rgba(0,0,0,.04);}
        .app-input:focus{outline:none;border-color:#10b981;box-shadow:0 0 0 3px rgba(16,185,129,.2);}
        .app-btn{display:inline-flex;align-items:center;justify-content:center;width:100%;border-radius:10px;padding:10px 14px;font-weight:800;transition:all .15s ease;cursor:pointer;}
        .app-btn-primary{background:linear-gradient(135deg,#059669 0%,#0d9488 100%);color:#fff;}
        .app-btn-primary:hover{background:linear-gradient(135deg,#047857 0%,#0f766e 100%);transform:translateY(-1px);box-shadow:0 4px 12px rgba(5,150,105,.3);}
        .app-btn-secondary{background:#fff;color:#047857;border:1px solid #6ee7b7;}
        .app-btn-secondary:hover{background:#ecfdf5;}
        .app-btn-amber{background:linear-gradient(135deg,#f59e0b 0%,#d97706 100%);color:#fff;}
        .app-btn-amber:hover{background:linear-gradient(135deg,#d97706 0%,#b45309 100%);transform:translateY(-1px);box-shadow:0 4px 12px rgba(245,158,11,.3);}
        .app-btn:disabled{opacity:.5;cursor:not-allowed;}
        .app-row{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
        .app-space{margin-bottom:14px;}
        .app-small{font-size:12px;opacity:.9;}
        .role-card{background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:18px;transition:all .2s ease;display:block;text-decoration:none;color:inherit;}
        .role-card:hover{border-color:#10b981;transform:translateY(-2px);box-shadow:0 8px 20px rgba(16,185,129,.15);}
        .role-card-amber:hover{border-color:#f59e0b;box-shadow:0 8px 20px rgba(245,158,11,.15);}
        .engine-pill{display:inline-flex;align-items:center;gap:4px;padding:3px 8px;border-radius:9999px;font-size:11px;font-weight:600;}
        .role-emoji{font-size:32px;line-height:1;margin-bottom:4px;display:block;}
        /* Cookie guide mock (self-contained DevTools schematic) */
        .cg-mock{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;color:#1f2937;font-size:11px;}
        .cg-titlebar{background:#e5e7eb;padding:5px 8px;display:flex;align-items:center;gap:6px;border-bottom:1px solid #d1d5db;}
        .cg-dot{width:9px;height:9px;border-radius:50%;display:inline-block;}
        .cg-r{background:#ef4444;} .cg-y{background:#eab308;} .cg-g{background:#22c55e;}
        .cg-url{flex:1;background:#fff;border:1px solid #d1d5db;border-radius:3px;padding:2px 8px;font-size:10.5px;color:#4b5563;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
        .cg-tabs{display:flex;background:#f3f4f6;border-bottom:1px solid #d1d5db;font-size:10.5px;color:#6b7280;overflow-x:auto;}
        .cg-tab{padding:5px 10px;border-right:1px solid #e5e7eb;white-space:nowrap;}
        .cg-tab.cg-on{background:#fff;color:#2563eb;font-weight:700;border-bottom:2px solid #2563eb;}
        .cg-body{display:flex;background:#fff;min-height:160px;}
        .cg-tree{flex:0 0 44%;border-right:1px solid #e5e7eb;padding:6px 8px;font-size:11px;line-height:1.55;color:#374151;}
        .cg-indent{padding-left:10px;}
        .cg-indent-2{padding-left:22px;}
        .cg-sel{background:#fef3c7;padding:1px 5px;border-radius:2px;color:#92400e;}
        .cg-tip{color:#9ca3af;font-size:10px;margin-left:2px;}
        .cg-table{flex:1;padding:0;font-size:11px;}
        .cg-th{background:#f9fafb;padding:4px 8px;font-weight:600;border-bottom:1px solid #e5e7eb;color:#6b7280;font-size:10.5px;}
        .cg-tr{padding:4px 8px;border-bottom:1px solid #f3f4f6;display:flex;align-items:center;gap:6px;}
        .cg-tr.cg-hl{background:#fef3c7;}
        .cg-tr.cg-hl b{color:#b45309;}
        .cg-val{color:#9ca3af;font-size:10px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
        .cg-tag{background:#fbbf24;color:#78350f;font-size:9.5px;padding:1px 6px;border-radius:8px;font-weight:700;letter-spacing:.5px;flex-shrink:0;}
    </style>
</head>
<body class="app-body p-4">
<div class="max-w-4xl mx-auto py-6 space-y-4">

    <!-- 顶部品牌 -->
    <div class="app-card text-center">
        <div class="inline-block px-3 py-1 bg-emerald-100 text-emerald-700 text-xs rounded-full mb-2">v3.5.2 · 选手成长平台</div>
        <h1 class="app-title">🏆 信竞 AI 报告 · 选手成长平台</h1>
        <p class="app-subtitle">OI 生涯决策助手 + 答疑讲题成长引擎</p>
        <p class="app-tag">从"一次性报告"到"持续陪伴"——段位、赛事、错题、冲刺，全在一处</p>
        <div class="app-muted flex items-center justify-center gap-2">
            <span>QQ交流群：<span id="qqGroup" class="text-emerald-700 font-semibold select-all">610931699</span></span>
            <button id="copyQqBtn" type="button" class="px-2 py-0.5 rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50 text-xs">复制</button>
        </div>
    </div>

    <!-- v3.5.2 主 CTA · 统一"AI 生成学习报告"入口 -->
    <div class="app-card bg-gradient-to-r from-emerald-50 to-cyan-50 border-2 border-emerald-300">
        <div class="text-center mb-3">
            <div class="inline-block px-3 py-1 bg-emerald-100 text-emerald-700 text-xs rounded-full mb-2">🎯 v3.5.2 统一入口</div>
            <h2 class="text-xl font-extrabold text-emerald-900 mb-1">🎓 AI 生成学习报告</h2>
            <p class="text-xs text-gray-600">输入洛谷账号 + 报告信息 · 30 秒出报告 · 含注册·错题本·3 版本报告</p>
        </div>
        <a href="/generate-form" class="app-btn app-btn-primary text-base py-3">
            🚀 立即生成我的学习报告
        </a>
        <p class="text-center text-xs text-gray-500 mt-2">报名信息（UID/姓名/学校/年级/城市）一次性填写，无需先注册</p>
        <!-- 老用户快速入口（不生成新报告，直接看） -->
        <a href="/select-mode" class="block text-center text-xs text-emerald-700 hover:underline mt-2">👀 我已注册 · 只想看历史报告 →</a>
    </div>

    <!-- 3 身份入口（已有账号/有身份 · 压缩为副入口） -->
    <div class="app-card">
        <h2 class="text-sm font-bold text-gray-700 mb-3">👋 已有账号？</h2>
        <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">

            <a href="/register" class="role-card text-center">
                <span class="role-emoji">🎓</span>
                <div class="font-bold text-gray-800 mb-1">我是选手</div>
                <div class="text-xs text-gray-500 mb-2">4 字段极简注册<br>段位查询 · 错题本</div>
                <span class="engine-pill bg-emerald-100 text-emerald-700">基本功能免费</span>
            </a>

            <a href="/parent" class="role-card text-center role-card-amber">
                <span class="role-emoji">👨‍👩‍👧</span>
                <div class="font-bold text-gray-800 mb-1">我是家长</div>
                <div class="text-xs text-gray-500 mb-2">完整 OI 报告<br>周报 · 倒推 · 政策</div>
                <span class="engine-pill bg-amber-100 text-amber-700">加 V 兑换码</span>
            </a>

            <a href="/coach" class="role-card text-center">
                <span class="role-emoji">🎯</span>
                <div class="font-bold text-gray-800 mb-1">我是教练</div>
                <div class="text-xs text-gray-500 mb-2">批量学员管理<br>兑换码生成 · 看板</div>
                <span class="engine-pill bg-gray-200 text-gray-700">联系客服购买</span>
            </a>
        </div>

        <!-- 学员 UID 快速入口（保留 · 用于老用户/已注册用户） -->
        <form id="me-entry" action="/me/0" method="get" class="mt-4 flex gap-2" onsubmit="event.preventDefault(); var u=document.getElementById('meUid').value.trim(); if(u && /^\d{6,10}$/.test(u)) window.location.href='/me/'+u; else alert('请输入 6-10 位洛谷 UID');">
            <input id="meUid" type="text" inputmode="numeric" pattern="\\d{6,10}" placeholder="洛谷 UID 6-10 位（已注册用户直接进入个人中心）" class="app-input flex-1">
            <button type="submit" class="app-btn app-btn-secondary px-4 whitespace-nowrap">进入</button>
        </form>
    </div>

    <!-- 3 大引擎（价值感知 · 替代价格表） -->
    <div class="app-card">
        <h2 class="text-sm font-bold text-gray-700 mb-3">⚡ 三大成长引擎</h2>
        <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
            <div class="app-box border-emerald-200 bg-emerald-50/50">
                <div class="text-lg mb-1">① GESP 跳级 + 免初赛</div>
                <p class="text-xs text-gray-600">90 分跳一级 · 7 级 80+ 免 CSP-J 初赛 · 8 级 80+ 免 CSP-S 初赛</p>
            </div>
            <div class="app-box border-blue-200 bg-blue-50/50">
                <div class="text-lg mb-1">② 固定赛事日历</div>
                <p class="text-xs text-gray-600">GESP 4 次 + CSP-J/S + NOIP + NOI · 倒推计划</p>
            </div>
            <div class="app-box border-purple-200 bg-purple-50/50">
                <div class="text-lg mb-1">③ StudyMate AI 讲题</div>
                <p class="text-xs text-gray-600">错题本一键跳转 AI 讲解 · 学员 Pro 专享</p>
            </div>
        </div>
    </div>

    <!-- 加 V 与客服（v3.5.2 唯一购买通道） -->
    <div class="app-card">
        <h2 class="text-sm font-bold text-gray-700 mb-3">💬 加 V 获取兑换码（家长/讲题）</h2>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div class="app-box border-amber-200 bg-amber-50/40">
                <div class="text-sm font-bold text-amber-800 mb-1">📱 加 V 通道（家长/讲题）</div>
                <p class="text-xs text-amber-700 mb-2">加客服微信，回复「家长」或「讲题」领取兑换码</p>
                <div class="flex items-center gap-2 bg-white border border-amber-200 rounded-lg px-3 py-2">
                    <span class="text-xs text-gray-500">微信号：</span>
                    <span class="font-mono font-bold text-amber-700 select-all" id="wechatVip">xinjing-ai-vip</span>
                    <button id="copyVipBtn" type="button" class="ml-auto px-2 py-0.5 rounded-md border border-amber-300 text-amber-700 hover:bg-amber-50 text-xs">复制</button>
                </div>
                <p class="text-xs text-gray-400 mt-2">工作日 9:00-21:00 · 节假日 10:00-18:00</p>
            </div>
            <div class="app-box border-gray-200 bg-gray-50/40">
                <div class="text-sm font-bold text-gray-800 mb-1">🏢 教练版咨询（机构/工作室）</div>
                <p class="text-xs text-gray-700 mb-2">批量学员管理 · 兑换码生成 · 营收看板</p>
                <div class="space-y-1 text-xs text-gray-700">
                    <div>📞 电话：<span class="font-mono font-semibold">400-XXX-XXXX</span></div>
                    <div>📧 邮箱：<span class="font-mono font-semibold">coach@xinjing-ai.com</span></div>
                    <div>🆚 备注：教练版按学员数计费，<a href="/coach" class="text-emerald-600 hover:underline">查看详情</a></div>
                </div>
            </div>
        </div>
    </div>

    <!-- v1 一次性测评报告（折叠保留 · v3.5.2 兜底） -->
    <div class="app-card bg-amber-50 border border-amber-200 p-3 mb-2">
        <p class="text-xs text-amber-800">
            ℹ️ <strong>新用户说明</strong>：v3.5.2 主入口已上移至顶部"查看我的学习报告"（需 4 字段注册）。
            本节是 <strong>v1 旧版一次性报告</strong>，适合不想注册的临时用户直接输入洛谷 Cookies + OpenAI 配置生成。
        </p>
    </div>
    <details id="v1-generate" class="app-card scroll-mt-4">
        <summary class="cursor-pointer text-sm font-bold text-gray-600 hover:text-emerald-600">📊 给新用户：一次性 AI 测评报告生成（v1 旧版 · 输入洛谷 Cookies + OpenAI 即出报告，无需注册）</summary>
        <div class="mt-4 pt-4 border-t border-gray-200">
            {% if validation_result %}
            <div class="app-box rounded-md p-3 mb-4 text-sm {% if validation_result.ok %}app-box-green bg-green-50 border border-green-200 text-green-800{% else %}app-box-red bg-red-50 border border-red-200 text-red-800{% endif %}">
                <p class="font-semibold mb-1">{{ validation_result.title }}</p>
                <p>{{ validation_result.message }}</p>
            </div>
            {% endif %}
            <div class="app-box app-box-yellow bg-yellow-50 border border-yellow-200 rounded-md p-3 mb-4 text-sm text-yellow-800">
                <p class="font-semibold mb-1">如何获取洛谷 Cookies：</p>
                <ol class="list-decimal list-inside space-y-1 text-xs text-yellow-700">
                    <li>打开 <code>https://www.luogu.com.cn</code> 并登录</li>
                    <li>按 <kbd class="px-1 bg-yellow-100 rounded">F12</kbd> → <kbd class="px-1 bg-yellow-100 rounded">Application(应用)</kbd> → <kbd class="px-1 bg-yellow-100 rounded">Storage → Cookies</kbd> → <code>https://www.luogu.com.cn</code></li>
                    <li>复制以下三个参数的 Name/Value 填入下方：</li>
                </ol>
            </div>
            <form action="/generate" method="post" class="space-y-4">
                <input type="hidden" name="resume_task_id" value="{{ form_values.resume_task_id }}">
                <div>
                    <label class="app-label block text-sm font-medium text-gray-700">__client_id</label>
                    <input type="text" name="client_id" value="{{ form_values.client_id }}" required class="app-input mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-emerald-500 focus:ring-emerald-500 border p-2">
                </div>
                <div>
                    <label class="app-label block text-sm font-medium text-gray-700">_uid</label>
                    <input type="text" name="uid" value="{{ form_values.uid }}" required class="app-input mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-emerald-500 focus:ring-emerald-500 border p-2">
                </div>
                <div>
                    <label class="app-label block text-sm font-medium text-gray-700">C3VK</label>
                    <input type="text" name="c3vk" value="{{ form_values.c3vk }}" required class="app-input mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-emerald-500 focus:ring-emerald-500 border p-2">
                </div>
                <div class="app-box app-box-blue bg-blue-50 border border-blue-200 rounded-md p-3">
                    <div class="flex items-start justify-between gap-3">
                        <div>
                            <p class="font-semibold mb-1">先校验 Cookies（推荐）</p>
                            <p class="text-xs text-blue-700">填写完上面三个参数后点一次，立刻检查 me / practice / record/list 是否可用。</p>
                        </div>
                    </div>
                    <div class="mt-3">
                        <button id="validateBtn" type="submit" formaction="/validate-cookies" class="app-btn app-btn-secondary w-full bg-white text-blue-700 font-semibold py-2 px-4 rounded-md border border-blue-300 hover:bg-blue-50 transition">校验 Cookies</button>
                    </div>
                    <p id="validateHint" class="app-small text-xs text-blue-700 mt-2">请先填写 __client_id、_uid、C3VK 后再校验。</p>
                </div>
                <div>
                    <label class="app-label block text-sm font-medium text-gray-700">OpenAI API Key（留空使用服务端默认）</label>
                    <input type="password" name="api_key" value="{{ form_values.api_key }}" class="app-input mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-emerald-500 focus:ring-emerald-500 border p-2">
                    <p class="text-xs text-gray-500 mt-1">{{ server_key_hint }}</p>
                </div>
                <div>
                    <label class="app-label block text-sm font-medium text-gray-700">API Base URL（留空使用服务端默认）</label>
                    <input type="text" name="base_url" value="{{ form_values.base_url }}" class="app-input mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-emerald-500 focus:ring-emerald-500 border p-2">
                </div>
                <div>
                    <label class="app-label block text-sm font-medium text-gray-700">模型名称（留空使用服务端默认）</label>
                    <input type="text" name="model_name" value="{{ form_values.model_name }}" class="app-input mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-emerald-500 focus:ring-emerald-500 border p-2">
                </div>
                <div class="app-row">
                    <div>
                        <label class="app-label block text-sm font-medium text-gray-700">姓名</label>
                        <input type="text" name="student_name" value="{{ form_values.student_name }}" class="app-input mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-emerald-500 focus:ring-emerald-500 border p-2">
                    </div>
                    <div>
                        <label class="app-label block text-sm font-medium text-gray-700">学校</label>
                        <input type="text" name="school" value="{{ form_values.school }}" class="app-input mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-emerald-500 focus:ring-emerald-500 border p-2">
                    </div>
                </div>
                <div>
                    <label class="app-label block text-sm font-medium text-gray-700">年级</label>
                    <input type="text" name="grade" value="{{ form_values.grade }}" class="app-input mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-emerald-500 focus:ring-emerald-500 border p-2">
                </div>
                <input type="hidden" name="max_passed" value="{{ form_values.max_passed }}">
                <input type="hidden" name="max_failed" value="{{ form_values.max_failed }}">
                <button id="generateBtn" type="submit" class="app-btn app-btn-primary w-full font-semibold py-3 px-4 rounded-md">生成报告</button>
            </form>
        </div>
    </details>

    <!-- 底部 -->
    <div class="text-center text-xs text-gray-400 py-4 space-x-3">
        <span>教练入口 → <a href="/admin/login" class="text-gray-500 hover:text-emerald-600 hover:underline">/admin/login</a></span>
        <span class="text-gray-300">·</span>
        <span>信竞 AI 报告 · v3.5.2 · 信奥选手成长平台</span>
    </div>

</div>

<script>
    (function () {
        function v(id) { var el = document.querySelector('input[name="' + id + '"]'); return el ? (el.value || '').trim() : ''; }
        var btn = document.getElementById('validateBtn');
        var hint = document.getElementById('validateHint');
        function refresh() {
            var ok = !!v('client_id') && !!v('uid') && !!v('c3vk');
            if (btn) btn.disabled = !ok;
            if (hint) hint.textContent = ok ? '已填写三个参数，建议先点一次校验。' : '请先填写 __client_id、_uid、C3VK 后再校验。';
        }
        ['client_id','uid','c3vk'].forEach(function (name) {
            var el = document.querySelector('input[name="' + name + '"]');
            if (el) el.addEventListener('input', refresh);
        });
        refresh();
    })();
    (function () {
        var btn = document.getElementById('copyQqBtn');
        var textEl = document.getElementById('qqGroup');
        if (!btn || !textEl) return;
        btn.addEventListener('click', async function () {
            var value = (textEl.textContent || '').trim();
            try {
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    await navigator.clipboard.writeText(value);
                } else {
                    var ta = document.createElement('textarea');
                    ta.value = value;
                    ta.style.position = 'fixed';
                    ta.style.top = '-1000px';
                    document.body.appendChild(ta);
                    ta.focus();
                    ta.select();
                    document.execCommand('copy');
                    document.body.removeChild(ta);
                }
                btn.textContent = '已复制';
                setTimeout(function () { btn.textContent = '复制'; }, 1200);
            } catch (e) {
                btn.textContent = '复制失败';
                setTimeout(function () { btn.textContent = '复制'; }, 1200);
            }
        });
    })();
    (function () {
        var btn = document.getElementById('copyVipBtn');
        var textEl = document.getElementById('wechatVip');
        if (!btn || !textEl) return;
        btn.addEventListener('click', async function () {
            var value = (textEl.textContent || '').trim();
            try {
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    await navigator.clipboard.writeText(value);
                } else {
                    var ta = document.createElement('textarea');
                    ta.value = value;
                    ta.style.position = 'fixed';
                    ta.style.top = '-1000px';
                    document.body.appendChild(ta);
                    ta.focus();
                    ta.select();
                    document.execCommand('copy');
                    document.body.removeChild(ta);
                }
                btn.textContent = '已复制';
                setTimeout(function () { btn.textContent = '复制'; }, 1200);
            } catch (e) {
                btn.textContent = '复制失败';
                setTimeout(function () { btn.textContent = '复制'; }, 1200);
            }
        });
    })();
</script>
</body>
</html>
"""


def build_cookie_dict(form: dict) -> dict[str, str]:
    client_id = str(form.get("client_id", "")).strip()
    uid = str(form.get("uid", "")).strip()
    c3vk = str(form.get("c3vk", "")).strip()
    missing = []
    if not client_id:
        missing.append("__client_id")
    if not uid:
        missing.append("_uid")
    if not c3vk:
        missing.append("C3VK")
    if missing:
        raise ValueError(f"Cookies 参数为必填项，请完整填写：{', '.join(missing)}")
    return {
        "__client_id": client_id,
        "_uid": uid,
        "C3VK": c3vk,
    }


RETRY_FORM_FIELDS = (
    "client_id",
    "uid",
    "c3vk",
    "api_key",
    "base_url",
    "model_name",
    "student_name",
    "school",
    "grade",
    "max_passed",
    "max_failed",
)


def build_retry_form_snapshot(form: dict | None = None) -> dict[str, str]:
    src = form or {}
    return {field: str(src.get(field, "") or "") for field in RETRY_FORM_FIELDS}


def load_retry_form_snapshot(task: dict | None) -> dict[str, str]:
    if not isinstance(task, dict):
        return {}
    raw_json = str(task.get("retry_form_json", "") or "").strip()
    if not raw_json:
        return {}
    try:
        payload = json.loads(raw_json)
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return build_retry_form_snapshot(payload)


def can_resume_from_ai_stage(task: dict | None) -> bool:
    if not isinstance(task, dict):
        return False
    if str(task.get("status", "") or "") == "done":
        return False
    try:
        report_dir = _resolve_task_report_dir(task)
    except Exception:
        return False
    return (report_dir / "export_data.json").exists()


def load_resume_export_data(task_id: str) -> tuple[dict | None, dict | None]:
    resume_task_id = str(task_id or "").strip()
    if not resume_task_id:
        return None, None
    task = get_task(resume_task_id)
    if not can_resume_from_ai_stage(task):
        return None, task
    try:
        report_dir = _resolve_task_report_dir(task)
        export_json_path = report_dir / "export_data.json"
        export_data = json.loads(export_json_path.read_text(encoding="utf-8"))
    except Exception:
        return None, task
    if not isinstance(export_data, dict):
        return None, task
    return export_data, task


def build_form_values(form: dict | None = None) -> dict[str, str]:
    src = form or {}
    return {
        "client_id": str(src.get("client_id", "")),
        "uid": str(src.get("uid", "")),
        "c3vk": str(src.get("c3vk", "")),
        "api_key": str(src.get("api_key", "")),
        "base_url": str(src.get("base_url", "")),
        "model_name": str(src.get("model_name", "")),
        "student_name": str(src.get("student_name", "未知选手")),
        "school": str(src.get("school", "未知学校")),
        "grade": str(src.get("grade", "未知年级")),
        "max_passed": str(src.get("max_passed", "5000")),
        "max_failed": str(src.get("max_failed", "1000")),
        "resume_task_id": str(src.get("resume_task_id", "")),
    }


def _get_server_key_hint() -> str:
    """v3.5.2 辅助函数：返回 OpenAI Key 服务端状态提示"""
    _, key_source = resolve_openai_api_key({})
    if key_source.startswith("env:"):
        return f"已检测到服务端 {key_source.split(':', 1)[1]}，可留空使用服务端默认。"
    return "未检测到服务端 OpenAI Key（可在服务端设置 OPENAI_API_KEY / OPENAI_ADMIN_KEY）。"


def render_index(form: dict | None = None, validation_result: dict | None = None):
    _, key_source = resolve_openai_api_key(form or {})
    if key_source.startswith("env:"):
        server_key_hint = f"已检测到服务端 {key_source.split(':', 1)[1]}，可留空使用服务端默认。"
    else:
        server_key_hint = "未检测到服务端 OpenAI Key（可在服务端设置 OPENAI_API_KEY / OPENAI_ADMIN_KEY）。"
    return render_template_string(
        INDEX_HTML,
        form_values=build_form_values(form),
        validation_result=validation_result,
        server_key_hint=server_key_hint,
    )


def _resolve_source_code_progress(export_data: dict | None) -> tuple[int, int]:
    if not isinstance(export_data, dict):
        return 0, 0
    detail_fetch_stats = export_data.get("detail_fetch_stats", {})
    if isinstance(detail_fetch_stats, dict):
        total_items = int(detail_fetch_stats.get("total_items") or 0)
        source_code_success = int(detail_fetch_stats.get("source_code_success") or 0)
        if total_items > 0:
            if source_code_success <= 0:
                source_code_success = total_items
            return source_code_success, total_items

    passed_items = export_data.get("passed_items", [])
    failed_items = export_data.get("failed_items", [])
    total_items = len(passed_items) + len(failed_items)
    if total_items <= 0:
        return 0, 0
    return total_items, total_items


def _build_report_paths(task_id: str, student_name: str) -> tuple[Path, Path, Path, Path, Path]:
    safe_name = "".join(c for c in student_name if c.isalnum() or c in "_-").strip() or "unknown"
    folder_name = f"{task_id[:8]}_{safe_name}"
    out_dir = Path("reports") / folder_name
    out_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = out_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    return (
        out_dir,
        assets_dir,
        out_dir / "report.md",
        out_dir / "report.html",
        out_dir / "report.pdf",
    )


def _write_export_data_json(out_dir: Path, export_data: dict) -> Path:
    export_json_path = out_dir / "export_data.json"
    with open(export_json_path, "w", encoding="utf-8") as f:
        json.dump(export_data, f, ensure_ascii=False, indent=2)
    return export_json_path


def _generate_ai_report_artifacts(
    task_id: str,
    export_data: dict,
    api_key: str,
    api_key_source: str,
    base_url: str | None,
    model_name: str,
    md_path: Path,
    html_path: Path,
    pdf_path: Path,
    assets_dir: Path,
    student_name: str,
    school: str,
    grade: str,
    resume_md_prefix: str | None = None,
) -> None:
    current_stage = "生成 AI 报告"
    with TASKS_LOCK:
        update_task(
            task_id,
            stage=current_stage,
            ai_progress=1,
            ai_elapsed_seconds=0,
            message=(
                f"正在调用 {model_name} 生成 AI 报告（流式写入，断连可自动续写）..."
                if not resume_md_prefix
                else f"正在续写 AI 报告（已加载 {len(resume_md_prefix)} 字符前置内容）..."
            ),
        )
    if not str(api_key or "").strip():
        raise ValueError("未配置 OpenAI API Key：请在页面填写 OpenAI API Key，或在服务端设置环境变量 OPENAI_API_KEY / OPENAI_ADMIN_KEY，并重启服务使其生效。")
    ai_holder: dict[str, object] = {
        "done": False,
        "report_md": None,
        "exc": None,
        "attempt": 1,
        "status_message": (
            f"正在调用 {model_name} 生成 AI 报告（流式写入，断连可自动续写）..."
            if not resume_md_prefix
            else f"正在续写 AI 报告（已加载 {len(resume_md_prefix)} 字符前置内容）..."
        ),
    }

    def _run_ai():
        attempt = 1
        # 当前 effective 的续写前缀：第 1 次用入参给的，后面会读 partial 接力
        current_resume = resume_md_prefix or ""
        while attempt <= AI_GENERATION_MAX_RETRIES:
            ai_holder["attempt"] = attempt
            ai_holder["status_message"] = (
                f"正在调用 {model_name} 生成 AI 报告（第 {attempt}/{AI_GENERATION_MAX_RETRIES} 次）"
                + (f" [续写 {len(current_resume)} 字符]" if current_resume else "")
            )
            try:
                ai_holder["report_md"] = generate_ai_report(
                    export_data,
                    api_key,
                    base_url,
                    model_name,
                    output_path=str(md_path),
                    resume_prefix=current_resume or None,
                )
                ai_holder["exc"] = None
                break
            except Exception as exc:
                ai_holder["exc"] = exc
                # 失败时尝试从 md_path 读取这一轮写下的 partial，作为下一轮续写前缀
                try:
                    if md_path.exists() and md_path.is_file():
                        partial = md_path.read_text(encoding="utf-8")
                        if partial and partial.strip():
                            current_resume = partial
                except Exception:
                    pass
                if (attempt >= AI_GENERATION_MAX_RETRIES) or (not _is_retryable_ai_error(exc)):
                    break
                ai_holder["status_message"] = (
                    f"AI 接口暂时不可用，正在等待后自动重试（第 {attempt}/{AI_GENERATION_MAX_RETRIES} 次失败）：{exc}"
                    + (f" [已有 {len(current_resume)} 字符可续写]" if current_resume else "")
                )
                time.sleep(AI_GENERATION_RETRY_SLEEP_SECONDS * attempt)
                attempt += 1
                continue
        ai_holder["done"] = True

    ai_thread = threading.Thread(target=_run_ai, daemon=True)
    ai_thread.start()
    ai_start = time.time()
    last_update = 0.0
    ai_progress = 1
    while ai_thread.is_alive():
        elapsed = int(time.time() - ai_start)
        ai_progress = min(95, max(ai_progress, min(95, elapsed * 3)))
        if (time.time() - last_update) >= 3.0:
            last_update = time.time()
            with TASKS_LOCK:
                update_task(
                    task_id,
                    stage=current_stage,
                    ai_progress=int(ai_progress),
                    ai_elapsed_seconds=int(elapsed),
                    message=(
                        f"{str(ai_holder.get('status_message') or f'正在调用 {model_name} 生成 AI 报告...')} "
                        f"({api_key_source}) 已等待 {elapsed}s"
                    ),
                )
        time.sleep(1)

    ai_thread.join(timeout=0.1)
    if ai_holder.get("exc") is not None:
        raise ai_holder["exc"]  # type: ignore[misc]
    report_md = str(ai_holder.get("report_md") or "")
    # 兼容旧路径：若 generate_ai_report 没有 output_path，则这里补写一次
    if report_md and not (md_path.exists() and md_path.stat().st_size > 0):
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(report_md)
    else:
        # 新路径：流式写盘后，优先以文件内容为准（保证后续阶段读到的就是磁盘上的最终归一化结果）
        try:
            report_md = md_path.read_text(encoding="utf-8") or report_md
        except Exception:
            pass

    with TASKS_LOCK:
        update_task(
            task_id,
            stage=current_stage,
            ai_progress=100,
            ai_elapsed_seconds=int(time.time() - ai_start),
            message=f"AI 报告已生成（{api_key_source}），正在生成图表与 HTML/PDF...",
        )

    current_stage = "生成图表与 HTML/PDF"
    chart_paths = generate_chart_images(export_data, str(assets_dir))
    build_html_and_pdf(report_md, export_data, str(html_path), str(pdf_path), chart_paths)

    eval_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    with TASKS_LOCK:
        update_task(
            task_id,
            status="done",
            message="报告生成完成",
            html=_report_url(html_path),
            pdf=_report_url(pdf_path),
            md=_report_url(md_path),
            student_name=student_name,
            school=school,
            grade=grade,
            solved_count=int(export_data.get("solved_count") or 0),
            failed_count=int(export_data.get("failed_count") or 0),
            eval_time=eval_time,
        )


def validate_cookies(form: dict) -> dict[str, object]:
    current_stage = "构造 Cookies"
    luogu = None
    try:
        cookies = pyLuogu.LuoguCookies(build_cookie_dict(form))
        current_stage = "预检用户信息"
        luogu = pyLuogu.luoguAPI(cookies=cookies)
        me = luogu.me()
        uid = int(me.uid)

        current_stage = "预检做题记录权限"
        practice = luogu.get_user_practice(uid)
        solved, failed = split_practice_problems(practice)

        current_stage = "预检提交记录权限"
        record_list = luogu.get_record_list(page=1, uid=uid, user=str(uid))
        record_count = len(getattr(record_list, "records", []) or [])
        return {
            "ok": True,
            "title": "Cookies 校验通过",
            "message": (
                f"已通过 me()、practice 和 record/list 预检。"
                f"用户 ID: {uid}，已通过 {len(solved)} 题，未通过 {len(failed)} 题，"
                f"最近一页提交记录 {record_count} 条。"
            ),
        }
    except Exception as exc:
        return {
            "ok": False,
            "title": "Cookies 校验失败",
            "message": describe_generation_error(exc, current_stage),
        }
    finally:
        if luogu is not None:
            luogu.close()


def run_generation(task_id: str, form: dict):
    current_stage = "初始化"
    try:
        with TASKS_LOCK:
            update_task(task_id, status="running", message="正在连接洛谷 API...")

        api_key, api_key_source = resolve_openai_api_key(form)
        base_url = form.get("base_url", "").strip() or DEFAULT_BASE_URL or os.environ.get("OPENAI_BASE_URL", "") or None
        model_name = form.get("model_name", "").strip() or DEFAULT_MODEL_NAME or os.environ.get("OPENAI_MODEL_NAME", "") or "gpt-4o"
        max_passed = int(form.get("max_passed", 10))
        max_failed = int(form.get("max_failed", 5))
        student_name = form.get("student_name", "未知选手").strip()
        school = form.get("school", "未知学校").strip()
        grade = form.get("grade", "未知年级").strip()
        resume_task_id = str(form.get("resume_task_id", "") or "").strip()
        resume_export_data, resume_task = load_resume_export_data(resume_task_id)
        if resume_export_data is not None:
            student_info = resume_export_data.get("student_info", {})
            if isinstance(student_info, dict):
                student_name = str(student_info.get("name") or student_name or "未知选手").strip()
                school = str(student_info.get("school") or school or "未知学校").strip()
                grade = str(student_info.get("grade") or grade or "未知年级").strip()

        out_dir, assets_dir, md_path, html_path, pdf_path = _build_report_paths(task_id, student_name)

        if resume_export_data is not None:
            current_stage = "恢复 AI 阶段数据"
            source_code_success, source_code_total = _resolve_source_code_progress(resume_export_data)

            # 尝试从上次任务读取 AI 报告的 partial 内容（如果有的话），用作本次的续写前缀
            resume_md_prefix = ""
            try:
                if isinstance(resume_task, dict):
                    prev_report_dir = _resolve_task_report_dir(resume_task)
                    prev_md = prev_report_dir / "report.md"
                    if prev_md.exists() and prev_md.is_file() and prev_md.stat().st_size > 200:
                        prev_text = prev_md.read_text(encoding="utf-8")
                        if prev_text and prev_text.strip():
                            resume_md_prefix = prev_text
            except Exception:
                resume_md_prefix = ""

            with TASKS_LOCK:
                update_task(
                    task_id,
                    stage=current_stage,
                    source_code_success=source_code_success,
                    source_code_total=source_code_total,
                    message=(
                        f"检测到任务 {resume_task_id[:8]} 已完成前置数据准备，"
                        + (
                            f"上次 AI 已生成 {len(resume_md_prefix)} 字符，将自动续写..."
                            if resume_md_prefix
                            else "正在跳过洛谷抓取并直接续跑 AI 报告..."
                        )
                    ),
                    student_name=student_name,
                    school=school,
                    grade=grade,
                    solved_count=int(resume_export_data.get("solved_count") or 0),
                    failed_count=int(resume_export_data.get("failed_count") or 0),
                )
            _write_export_data_json(out_dir, resume_export_data)
            _generate_ai_report_artifacts(
                task_id=task_id,
                export_data=resume_export_data,
                api_key=api_key,
                api_key_source=api_key_source,
                base_url=base_url,
                model_name=model_name,
                md_path=md_path,
                html_path=html_path,
                pdf_path=pdf_path,
                assets_dir=assets_dir,
                student_name=student_name,
                school=school,
                grade=grade,
                resume_md_prefix=resume_md_prefix or None,
            )
            return

        current_stage = "构造 Cookies"
        cookies = pyLuogu.LuoguCookies(build_cookie_dict(form))

        current_stage = "连接洛谷 API / me()"
        luogu = pyLuogu.luoguAPI(cookies=cookies)
        me = luogu.me()
        uid = int(me.uid)

        with TASKS_LOCK:
            update_task(task_id, message=f"已连接，用户 ID: {uid}，正在拉取做题记录...")

        current_stage = "获取标签与练习数据"
        tag_by_id, type_by_id = _build_tag_maps(luogu)
        practice = luogu.get_user_practice(uid)

        current_stage = "预检提交记录权限"
        luogu.get_record_list(page=1, uid=uid, user=str(uid))

        all_passed, all_failed = split_practice_problems(practice)

        # 预判：是否需要补全标签？避免无意义地卡在"正在补全题目标签数据..."上
        tag_already_present = sum(
            1 for p in all_passed if list(getattr(p, "tags", []) or [])
        )
        tag_missing = len(all_passed) - tag_already_present
        current_stage = "补全题目标签"

        # 标签补全进度回调
        tag_last_update = [0.0]

        def _on_tag_progress(fetched: int, enriched: int, total_missing: int) -> None:
            if total_missing <= 0:
                return
            now = time.time()
            # 限制更新频率：每 0.4s 或每 5 题一次
            if (now - tag_last_update[0]) < 0.4 and fetched % 5 != 0 and fetched != total_missing:
                return
            tag_last_update[0] = now
            with TASKS_LOCK:
                update_task(
                    task_id,
                    message=(
                        f"正在补全题目标签（按需抓取，最慢的阶段）... "
                        f"已补全 {enriched}/{total_missing} 题（已抓 {fetched} 题详情）"
                    ),
                    stage=current_stage,
                    tag_fetch_success=enriched,
                    tag_fetch_total=total_missing,
                )

        if tag_missing > 0:
            with TASKS_LOCK:
                update_task(
                    task_id,
                    message=(
                        f"正在补全题目标签（按需抓取，最慢的阶段）... "
                        f"0/{tag_missing} 题"
                    ),
                    stage=current_stage,
                    tag_fetch_success=0,
                    tag_fetch_total=tag_missing,
                )
            enrich_problem_tags(luogu, all_passed, progress_callback=_on_tag_progress)
        else:
            with TASKS_LOCK:
                update_task(
                    task_id,
                    message=(
                        f"题目标签已齐全（{tag_already_present} 题均有标签，无需补全）"
                    ),
                    stage=current_stage,
                    tag_fetch_success=0,
                    tag_fetch_total=0,
                )

        all_passed.sort(key=lambda p: (p.difficulty if p.difficulty is not None else 10, p.pid), reverse=True)
        all_failed.sort(key=lambda p: (p.difficulty if p.difficulty is not None else 10, p.pid), reverse=True)
        passed_problems = all_passed[:max_passed]
        failed_problems = all_failed[:max_failed]

        with TASKS_LOCK:
            update_task(task_id, message="正在拉取提交记录与代码...")

        current_stage = "抓取提交记录与代码"
        source_code_total = int(len(passed_problems) + len(failed_problems))
        source_code_success = 0
        processed = 0
        last_progress_update = 0.0
        cached_source_hits = 0

        def _is_source_code_present(record_obj: object) -> bool:
            if isinstance(record_obj, dict):
                code = record_obj.get("sourceCode")
                return bool(code)
            return False

        def _maybe_update_progress(force: bool = False) -> None:
            nonlocal last_progress_update
            now = time.time()
            if (not force) and (now - last_progress_update) < 2.0 and (processed % 10 != 0):
                return
            last_progress_update = now
            msg = (
                f"正在拉取提交记录与代码（源码优先，速度较慢）... "
                f"已获取源码 {source_code_success}/{source_code_total}，"
                f"其中复用缓存 {cached_source_hits} 题，进度 {processed}/{source_code_total}"
            )
            with TASKS_LOCK:
                update_task(
                    task_id,
                    message=msg,
                    stage=current_stage,
                    source_code_success=source_code_success,
                    source_code_total=source_code_total,
                )

        detail_fetch_state: dict[str, object] = {}
        passed_items = []
        pending_passed_problems = []
        pending_failed_problems = []

        def _prefill_cached_records(problems: list[object], target_items: list[dict]) -> list[object]:
            nonlocal processed, source_code_success, cached_source_hits
            remaining = []
            for problem in problems:
                cached_record = load_cached_source_record(uid, getattr(problem, "pid", ""))
                if cached_record is None:
                    remaining.append(problem)
                    continue
                target_items.append({"problem": problem.to_json(), "record": cached_record})
                processed += 1
                cached_source_hits += 1
                if _is_source_code_present(cached_record):
                    source_code_success += 1
            return remaining

        pending_passed_problems = _prefill_cached_records(passed_problems, passed_items)
        failed_items = []
        pending_failed_problems = _prefill_cached_records(failed_problems, failed_items)

        with TASKS_LOCK:
            update_task(
                task_id,
                message=(
                    "正在拉取提交记录与代码（源码优先，速度较慢，支持缓存复用）... "
                    f"已预载缓存 {cached_source_hits} 题源码，剩余待抓取 "
                    f"{source_code_total - processed} 题"
                ),
                stage=current_stage,
                source_code_success=source_code_success,
                source_code_total=source_code_total,
            )
        _maybe_update_progress(force=True)

        # 并发抓取源码：4 worker，httpx.Client 线程安全；detail_fetch_state 复制到线程内避免跨线程改
        from concurrent.futures import ThreadPoolExecutor, as_completed

        SOURCE_FETCH_CONCURRENCY = 4
        _state_lock = threading.Lock()

        def _fetch_one_record(problem):
            state_snapshot = dict(detail_fetch_state)
            try:
                record = _pick_record_for_problem(
                    luogu=luogu,
                    uid=uid,
                    pid=problem.pid,
                    max_records_to_try=5,
                    require_source_code=True,
                    detail_fetch_state=state_snapshot,
                )
            except Exception as e:
                record = {"error": str(e)}
            # 跨线程合并 circuit breaker 状态
            with _state_lock:
                if state_snapshot.get("stop_detail_fetch") and not detail_fetch_state.get("stop_detail_fetch"):
                    detail_fetch_state["stop_detail_fetch"] = True
                    detail_fetch_state["last_detail_error"] = state_snapshot.get("last_detail_error")
            return record

        def _run_concurrently(problems, items_sink):
            nonlocal processed, source_code_success
            with ThreadPoolExecutor(max_workers=SOURCE_FETCH_CONCURRENCY) as ex:
                futures = {ex.submit(_fetch_one_record, p): p for p in problems}
                for fut in as_completed(futures):
                    problem = futures[fut]
                    try:
                        record = fut.result()
                    except Exception as e:
                        record = {"error": str(e)}
                    save_cached_source_record(uid, problem.pid, record if isinstance(record, dict) else None)
                    items_sink.append({"problem": problem.to_json(), "record": record})
                    processed += 1
                    if _is_source_code_present(record):
                        source_code_success += 1
                    _maybe_update_progress()

        _run_concurrently(pending_passed_problems, passed_items)
        _run_concurrently(pending_failed_problems, failed_items)

        _maybe_update_progress(force=True)
        detail_fetch_stats = summarize_detail_fetch_stats(passed_items, failed_items, detail_fetch_state)
        total_items = int(detail_fetch_stats.get("total_items") or 0)
        source_code_success = int(detail_fetch_stats.get("source_code_success") or 0)
        if total_items > 0 and source_code_success < total_items:
            raise RuntimeError(
                f"源码抓取未完成：成功 {source_code_success}/{total_items}。"
                f"本次已复用缓存 {cached_source_hits} 题源码。"
                f"已放慢抓取速度并提高重试，仍有缺失。"
                f"请重新获取同一会话下的 __client_id、_uid、C3VK 后重试；"
                f"必要时降低题量（max_passed/max_failed）以提高稳定性。"
            )

        summary = _summarize(all_passed, tag_by_id=tag_by_id)

        # ========== 新增：提交行为深度分析 ==========
        with TASKS_LOCK:
            update_task(task_id, message="正在进行提交行为深度分析...")

        current_stage = "提交行为分析"
        behavior_data = fetch_behavior_analysis(luogu, uid, passed_items + failed_items)
        behavior_data = repair_behavior_analysis_from_items(
            {
                "passed_items": passed_items,
                "failed_items": failed_items,
                "behavior_analysis": behavior_data,
            }
        )
        detail_fetch_stats = summarize_detail_fetch_stats(passed_items, failed_items, detail_fetch_state)

        # ========== 新增：大纲知识点对标 ==========
        with TASKS_LOCK:
            update_task(task_id, message="正在进行大纲知识点对标分析...")

        current_stage = "大纲知识点对标"
        syllabus_evaluation = evaluate_all_topics(summary.get("top_algorithm_tags", []) or summary.get("top_tags", []))
        six_dim_scores = compute_six_dimension_scores(
            {"solved_count": len(all_passed), "summary": summary},
            behavior_data if "error" not in behavior_data else {}
        )

        export_data = {
            "student_info": {
                "name": student_name,
                "school": school,
                "grade": grade,
                "eval_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            },
            "solved_count": len(all_passed),
            "failed_count": len(all_failed),
            "summary": summary,
            "passed_items": passed_items,
            "failed_items": failed_items,
            "detail_fetch_stats": detail_fetch_stats,
            "behavior_analysis": behavior_data,
            "syllabus_evaluation": syllabus_evaluation,
            "six_dimension_scores": six_dim_scores,
        }

        _write_export_data_json(out_dir, export_data)
        _generate_ai_report_artifacts(
            task_id=task_id,
            export_data=export_data,
            api_key=api_key,
            api_key_source=api_key_source,
            base_url=base_url,
            model_name=model_name,
            md_path=md_path,
            html_path=html_path,
            pdf_path=pdf_path,
            assets_dir=assets_dir,
            student_name=student_name,
            school=school,
            grade=grade,
        )
    except Exception as e:
        # 用 task 表里最新写入的 stage 替换外层缓存的 current_stage，
        # 避免出现 "[阶段: 恢复 AI 阶段数据] Connection error." 这种阶段名滞后的问题
        try:
            latest_task = get_task(task_id) or {}
            latest_stage = str(latest_task.get("stage") or current_stage or "")
        except Exception:
            latest_stage = current_stage
        with TASKS_LOCK:
            update_task(
                task_id,
                status="error",
                message=describe_generation_error(e, latest_stage),
            )
    finally:
        unregister_active_generation_task(task_id)


@app.route("/")
def index():
    return render_index()


@app.route("/validate-cookies", methods=["POST"])
def validate_cookies_page():
    form = request.form.to_dict()
    return render_index(form=form, validation_result=validate_cookies(form))


@app.route("/generate", methods=["POST"])
def generate():
    form_data = request.form.to_dict()
    task_id = str(uuid.uuid4())
    with TASKS_LOCK:
        insert_task(task_id, status="queued", message="排队中...")
        update_task(
            task_id,
            student_name=str(form_data.get("student_name", "未知选手") or "未知选手").strip(),
            school=str(form_data.get("school", "未知学校") or "未知学校").strip(),
            grade=str(form_data.get("grade", "未知年级") or "未知年级").strip(),
            retry_form_json=json.dumps(build_retry_form_snapshot(form_data), ensure_ascii=False),
        )
    thread = threading.Thread(target=run_generation, args=(task_id, form_data), daemon=True)
    register_active_generation_task(task_id, thread)
    thread.start()
    return redirect(url_for("status_page", task_id=task_id))


STATUS_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>报告生成状态</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <meta http-equiv="refresh" content="3">
</head>
<body class="bg-gray-100 min-h-screen flex items-center justify-center p-4">
    <div class="bg-white rounded-xl shadow-lg p-8 w-full max-w-lg text-center">
        <h1 class="text-2xl font-bold text-blue-900 mb-4">报告生成状态</h1>
        <div class="mb-4">
            <span class="inline-block px-3 py-1 rounded-full text-sm font-semibold
                {% if status == 'done' %}bg-green-100 text-green-800{% elif status == 'error' %}bg-red-100 text-red-800{% else %}bg-blue-100 text-blue-800{% endif %}">
                {{ status }}
            </span>
        </div>
        {% if source_code_total and source_code_total|int > 0 %}
        <div class="mb-4 text-left">
            <div class="flex items-center justify-between text-sm text-gray-600 mb-1">
                <span>源码获取进度</span>
                <span class="font-semibold text-gray-800">{{ source_code_success }}/{{ source_code_total }}</span>
            </div>
            <div class="w-full bg-gray-200 rounded-full h-3 overflow-hidden">
                <div class="bg-blue-600 h-3" style="width: {{ (100 * (source_code_success|int) / (source_code_total|int)) if (source_code_total|int) > 0 else 0 }}%;"></div>
            </div>
        </div>
        {% endif %}
        {% if tag_fetch_total and tag_fetch_total|int > 0 %}
        <div class="mb-4 text-left">
            <div class="flex items-center justify-between text-sm text-gray-600 mb-1">
                <span>标签补全进度</span>
                <span class="font-semibold text-gray-800">{{ tag_fetch_success }}/{{ tag_fetch_total }}</span>
            </div>
            <div class="w-full bg-gray-200 rounded-full h-3 overflow-hidden">
                <div class="bg-amber-500 h-3" style="width: {{ (100 * (tag_fetch_success|int) / (tag_fetch_total|int)) if (tag_fetch_total|int) > 0 else 0 }}%;"></div>
            </div>
        </div>
        {% endif %}
        {% if stage == '生成 AI 报告' %}
        <div class="mb-4 text-left">
            <div class="flex items-center justify-between text-sm text-gray-600 mb-1">
                <span>AI 报告生成进度</span>
                <span class="font-semibold text-gray-800">{{ ai_progress }}%{% if ai_elapsed_seconds and ai_elapsed_seconds|int > 0 %} · {{ ai_elapsed_seconds }}s{% endif %}</span>
            </div>
            <div class="w-full bg-gray-200 rounded-full h-3 overflow-hidden">
                <div class="bg-blue-600 h-3" style="width: {{ ai_progress|int }}%;"></div>
            </div>
        </div>
        {% endif %}
        <p class="text-gray-700 mb-6">{{ message }}</p>
        {% if status == 'done' %}
        <div class="space-y-3">
            <a href="{{ html }}" target="_blank" class="block w-full bg-blue-600 text-white font-semibold py-2 px-4 rounded-md hover:bg-blue-700 transition">查看 HTML 报告</a>
            <a href="{{ pdf }}" target="_blank" class="block w-full bg-gray-700 text-white font-semibold py-2 px-4 rounded-md hover:bg-gray-800 transition">下载 PDF 报告</a>
            <a href="{{ md }}" target="_blank" class="block w-full bg-gray-200 text-gray-800 font-semibold py-2 px-4 rounded-md hover:bg-gray-300 transition">查看 Markdown 原文</a>
            {% if me_url %}
            <a href="{{ me_url }}" class="block w-full bg-gradient-to-r from-emerald-500 to-cyan-500 text-white font-bold py-2.5 px-4 rounded-md hover:from-emerald-600 hover:to-cyan-600 transition">🎓 查看我的报告中心（3 版本）</a>
            {% endif %}
        </div>
        {% elif status == 'error' %}
        <a href="{{ retry_url }}" class="block w-full bg-blue-600 text-white font-semibold py-2 px-4 rounded-md hover:bg-blue-700 transition mt-4">返回重试</a>
        {% if me_url %}
        <a href="{{ me_url }}" class="block w-full bg-gradient-to-r from-emerald-500 to-cyan-500 text-white font-bold py-2.5 px-4 rounded-md hover:from-emerald-600 hover:to-cyan-600 transition mt-2">🎓 查看我的报告中心（3 版本）</a>
        {% endif %}
        {% else %}
        <p class="text-sm text-gray-400">页面每 3 秒自动刷新...</p>
        {% endif %}
    </div>
</body>
</html>
"""


@app.route("/status/<task_id>")
def status_page(task_id):
    if not is_generation_task_active(task_id):
        reconcile_stale_generation_tasks()
    task = get_task(task_id) or {"status": "unknown", "message": "任务不存在"}
    pdf_url = str(task.get("pdf", "") or "")
    # v3.5.2 · 统一入口生成的报告支持跳回 /me/<uid>（3 版本报告）
    luogu_uid = str(request.args.get("luogu_uid", "") or "")
    me_url = f"/me/{luogu_uid}" if luogu_uid and luogu_uid.isdigit() else ""
    return render_template_string(
        STATUS_HTML,
        status=task.get("status", "unknown"),
        message=task.get("message", ""),
        stage=str(task.get("stage", "") or ""),
        source_code_success=int(task.get("source_code_success", 0) or 0),
        source_code_total=int(task.get("source_code_total", 0) or 0),
        tag_fetch_success=int(task.get("tag_fetch_success", 0) or 0),
        tag_fetch_total=int(task.get("tag_fetch_total", 0) or 0),
        ai_progress=int(task.get("ai_progress", 0) or 0),
        ai_elapsed_seconds=int(task.get("ai_elapsed_seconds", 0) or 0),
        html=task.get("html", ""),
        pdf=_download_report_url(pdf_url),
        md=task.get("md", ""),
        retry_url=url_for("retry_task", task_id=task_id),
        me_url=me_url,
    )


@app.route("/retry/<task_id>")
def retry_task(task_id):
    task = get_task(task_id)
    snapshot = load_retry_form_snapshot(task)
    if not snapshot:
        return redirect("/")
    if can_resume_from_ai_stage(task):
        snapshot["resume_task_id"] = task_id
    return render_index(form=snapshot)


@app.route("/static/<path:filename>")
def serve_static(filename):
    """Serve static assets (e.g. cookies guide image) from the project root."""
    static_root = (_ROOT / "static").resolve()
    target = (static_root / filename).resolve()
    try:
        target.relative_to(static_root)
    except ValueError:
        return ("Forbidden", 403)
    if target.is_file():
        return send_file(str(target), conditional=True)
    return ("Not Found", 404)


@app.route("/reports/<path:filename>")
def serve_report(filename):
    report_root = (_ROOT / "reports").resolve()
    file_path = (report_root / filename).resolve()
    try:
        file_path.relative_to(report_root)
    except ValueError:
        return send_from_directory(str(report_root), filename)

    if file_path.suffix.lower() == ".pdf" and file_path.is_file():
        response = send_file(
            str(file_path),
            mimetype="application/pdf",
            as_attachment=False,
            conditional=True,
            etag=True,
            last_modified=file_path.stat().st_mtime,
        )
        response.headers["Content-Disposition"] = 'inline; filename="report.pdf"'
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    response = send_from_directory(str(report_root), filename)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.route("/download-report/<path:filename>")
def download_report(filename):
    report_root = (_ROOT / "reports").resolve()
    file_path = (report_root / filename).resolve()
    try:
        file_path.relative_to(report_root)
    except ValueError:
        return send_from_directory(str(report_root), filename, as_attachment=True)

    if file_path.is_file():
        response = send_file(
            str(file_path),
            as_attachment=True,
            download_name=file_path.name,
            conditional=False,
        )
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return response

    return send_from_directory(str(report_root), filename, as_attachment=True)


def _report_url(path: Path) -> str:
    path_obj = Path(path)
    if path_obj.is_absolute():
        try:
            path_obj = path_obj.resolve().relative_to(_ROOT.resolve())
        except Exception:
            path_obj = Path(path_obj.name)
    url = "/" + path_obj.as_posix()
    try:
        version = Path(path).stat().st_mtime_ns
    except OSError:
        return url
    return f"{url}?v={version}"


def _download_report_url(raw_url: str) -> str:
    value = str(raw_url or "").strip()
    if not value:
        return ""
    parts = urlsplit(value)
    path = parts.path
    if not path.startswith("/reports/"):
        return value
    download_path = "/download-report/" + path[len("/reports/"):]
    return urlunsplit((parts.scheme, parts.netloc, download_path, parts.query, parts.fragment))


def _resolve_task_report_dir(task: dict) -> Path:
    for field in ("html", "md", "pdf"):
        raw_value = str(task.get(field, "") or "").strip()
        if not raw_value:
            continue
        raw_path = urlsplit(raw_value).path or raw_value
        report_path = Path(raw_path.lstrip("/"))
        if not report_path.is_absolute():
            report_path = (_ROOT / report_path).resolve()
        if report_path.suffix:
            return report_path.parent
        return report_path
    task_id = str(task.get("task_id", "") or "").strip()
    if task_id:
        report_root = (_ROOT / "reports").resolve()
        prefix = task_id[:8]
        candidates = sorted(
            [path for path in report_root.glob(f"{prefix}_*") if path.is_dir()],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    raise FileNotFoundError("未找到该任务对应的报告目录")


def _existing_chart_paths(assets_dir: Path) -> dict[str, str]:
    mapping = {
        "difficulty": "difficulty_histogram.png",
        "status": "status_ratio.png",
        "tags": "top_tags.png",
        "radar": "ability_radar.png",
        "personality_radar": "personality_radar.png",
        "ac_submit_distribution": "ac_submit_distribution.png",
    }
    result: dict[str, str] = {}
    for key, filename in mapping.items():
        file_path = assets_dir / filename
        if file_path.exists():
            result[key] = str(file_path)
    return result


def rebuild_existing_report_html(task_id: str, export_pdf: bool = False) -> str:
    task = get_task(task_id)
    if not task:
        raise FileNotFoundError("任务不存在")

    report_dir = _resolve_task_report_dir(task)
    export_json_path = report_dir / "export_data.json"
    md_path = report_dir / "report.md"
    html_path = report_dir / "report.html"
    pdf_path = report_dir / "report.pdf"
    assets_dir = report_dir / "assets"

    if not export_json_path.exists():
        raise FileNotFoundError("缺少 export_data.json，无法重建 HTML")
    if not md_path.exists():
        raise FileNotFoundError("缺少 report.md，无法重建 HTML")

    export_data = json.loads(export_json_path.read_text(encoding="utf-8"))
    report_md = md_path.read_text(encoding="utf-8")
    assets_dir.mkdir(parents=True, exist_ok=True)
    chart_paths = generate_chart_images(export_data, str(assets_dir))
    build_html_and_pdf(
        report_md,
        export_data,
        str(html_path),
        str(pdf_path),
        chart_paths,
        export_pdf=export_pdf,
    )

    html_url = _report_url(html_path)
    md_url = _report_url(md_path)
    pdf_url = _report_url(pdf_path) if pdf_path.exists() else str(task.get("pdf", "") or "")
    with TASKS_LOCK:
        update_task(
            task_id,
            html=html_url,
            md=md_url,
            pdf=pdf_url,
            message="已重建 HTML/PDF 报告" if export_pdf else "已重建 HTML 报告",
        )
    return html_url


ADMIN_LOGIN_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>管理员登录 - 洛谷 AI 测评报告</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 min-h-screen flex items-center justify-center p-4">
    <div class="bg-white rounded-xl shadow-lg p-8 w-full max-w-md">
        <h1 class="text-2xl font-bold text-blue-900 mb-2">管理员登录</h1>
        <p class="text-sm text-gray-500 mb-6">请输入管理员账号和密码后访问后台管理页面。</p>
        {% if error %}
        <div class="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{{ error }}</div>
        {% endif %}
        {% if notice %}
        <div class="mb-4 rounded-lg border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-700">{{ notice }}</div>
        {% endif %}
        <form method="post" action="/admin/login" class="space-y-4">
            <input type="hidden" name="next" value="{{ next_url }}">
            <div>
                <label class="block text-sm font-medium text-gray-700">管理员账号</label>
                <input type="text" name="username" value="{{ username }}" required class="mt-1 block w-full rounded-md border border-gray-300 p-2 shadow-sm focus:border-blue-500 focus:ring-blue-500">
            </div>
            <div>
                <label class="block text-sm font-medium text-gray-700">管理员密码</label>
                <input type="password" name="password" required class="mt-1 block w-full rounded-md border border-gray-300 p-2 shadow-sm focus:border-blue-500 focus:ring-blue-500">
            </div>
            <button type="submit" class="w-full rounded-md bg-blue-600 px-4 py-2 font-semibold text-white hover:bg-blue-700 transition">登录后台</button>
        </form>
        <p class="mt-4 text-xs text-gray-400">可通过环境变量 `ADMIN_USERNAME`、`ADMIN_PASSWORD`、`ADMIN_SESSION_SECRET` 配置管理员登录。</p>
    </div>
</body>
</html>
"""


# ========== 后台管理页面 ==========
ADMIN_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>后台管理 - 洛谷 AI 测评报告</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <meta http-equiv="refresh" content="10">
</head>
<body class="bg-gray-100 min-h-screen p-6">
    <div class="max-w-6xl mx-auto">
        <div class="flex items-center justify-between mb-6">
            <h1 class="text-3xl font-bold text-blue-900">后台管理</h1>
            <div class="flex items-center gap-4">
                <span class="text-sm text-gray-500">管理员：{{ admin_user }}</span>
                <a href="/admin/students" class="text-blue-600 hover:underline">学员档案</a>
                <a href="/" class="text-blue-600 hover:underline">返回首页</a>
                <a href="/admin/logout" class="text-red-600 hover:underline">退出登录</a>
            </div>
        </div>
        {% if notice %}
        <div class="mb-4 rounded-lg border px-4 py-3 text-sm {% if notice_type == 'error' %}bg-red-50 border-red-200 text-red-700{% else %}bg-green-50 border-green-200 text-green-700{% endif %}">
            {{ notice }}
        </div>
        {% endif %}

        <!-- 统计卡片 -->
        <div class="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
            <div class="bg-white rounded-xl shadow p-4">
                <p class="text-sm text-gray-500">总生成次数</p>
                <p class="text-2xl font-bold text-blue-700">{{ total_tasks }}</p>
            </div>
            <div class="bg-white rounded-xl shadow p-4">
                <p class="text-sm text-gray-500">今日生成</p>
                <p class="text-2xl font-bold text-green-700">{{ today_tasks }}</p>
            </div>
            <div class="bg-white rounded-xl shadow p-4">
                <p class="text-sm text-gray-500">进行中</p>
                <p class="text-2xl font-bold text-yellow-600">{{ running_tasks }}</p>
            </div>
            <div class="bg-white rounded-xl shadow p-4">
                <p class="text-sm text-gray-500">失败次数</p>
                <p class="text-2xl font-bold text-red-600">{{ error_tasks }}</p>
            </div>
        </div>

        <!-- 历史任务列表 -->
        <div class="bg-white rounded-xl shadow overflow-hidden">
            <div class="px-6 py-4 border-b border-gray-200">
                <h2 class="text-lg font-semibold text-gray-800">历史任务列表</h2>
            </div>
            <div class="overflow-x-auto">
                <table class="min-w-full text-sm text-left">
                    <thead class="bg-gray-50 text-gray-600 font-medium">
                        <tr>
                            <th class="px-6 py-3">任务 ID</th>
                            <th class="px-6 py-3">姓名</th>
                            <th class="px-6 py-3">学校</th>
                            <th class="px-6 py-3">年级</th>
                            <th class="px-6 py-3">通过题数</th>
                            <th class="px-6 py-3">失败题数</th>
                            <th class="px-6 py-3">状态</th>
                            <th class="px-6 py-3">时间</th>
                            <th class="px-6 py-3">操作</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-gray-100">
                        {% for task in tasks %}
                        <tr class="hover:bg-gray-50">
                            <td class="px-6 py-3 font-mono text-xs text-gray-500">{{ task.id[:8] }}...</td>
                            <td class="px-6 py-3 font-medium text-gray-900">{{ task.name }}</td>
                            <td class="px-6 py-3 text-gray-600">{{ task.school }}</td>
                            <td class="px-6 py-3 text-gray-600">{{ task.grade }}</td>
                            <td class="px-6 py-3 text-green-700 font-semibold">{{ task.solved }}</td>
                            <td class="px-6 py-3 text-red-600 font-semibold">{{ task.failed }}</td>
                            <td class="px-6 py-3">
                                {% if task.status == 'done' %}
                                    <span class="px-2 py-1 rounded-full text-xs bg-green-100 text-green-800">完成</span>
                                {% elif task.status == 'error' %}
                                    <span class="px-2 py-1 rounded-full text-xs bg-red-100 text-red-800">失败</span>
                                {% elif task.status == 'running' %}
                                    <span class="px-2 py-1 rounded-full text-xs bg-yellow-100 text-yellow-800">进行中</span>
                                {% else %}
                                    <span class="px-2 py-1 rounded-full text-xs bg-gray-100 text-gray-800">{{ task.status }}</span>
                                {% endif %}
                            </td>
                            <td class="px-6 py-3 text-gray-500 text-xs">{{ task.time }}</td>
                            <td class="px-6 py-3 space-x-2">
                                {% if task.html %}
                                <a href="{{ task.html }}" target="_blank" class="text-blue-600 hover:underline text-xs">HTML</a>
                                {% endif %}
                                {% if task.pdf %}
                                <a href="{{ task.pdf }}" target="_blank" class="text-blue-600 hover:underline text-xs">下载PDF</a>
                                {% endif %}
                                {% if task.md %}
                                <a href="{{ task.md }}" target="_blank" class="text-blue-600 hover:underline text-xs">MD</a>
                                {% endif %}
                                {% if task.can_rebuild %}
                                <form method="post" action="/admin/rebuild-html/{{ task.id }}" class="inline">
                                    <button type="submit" class="text-xs text-indigo-600 hover:underline">重建 HTML</button>
                                </form>
                                {% endif %}
                                {% if task.rebuild_status == 'running' %}
                                <span class="text-xs text-amber-600">重建中...</span>
                                {% elif task.rebuild_status == 'done' %}
                                <span class="text-xs text-green-600">已重建</span>
                                {% elif task.rebuild_status == 'error' %}
                                <span class="text-xs text-red-600">重建失败</span>
                                {% endif %}
                                {% if task.rebuild_message %}
                                <span class="block mt-1 text-[11px] text-gray-500">{{ task.rebuild_message }}</span>
                                {% endif %}
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
    </div>
</body>
</html>
"""


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = str(request.form.get("username", "") or "").strip()
        password = str(request.form.get("password", "") or "")
        next_url = sanitize_admin_next(request.form.get("next"))
        if check_admin_credentials(username, password):
            session["admin_authed"] = True
            session["admin_user"] = username
            return redirect(next_url)
        return render_template_string(
            ADMIN_LOGIN_HTML,
            error="账号或密码错误，请重新输入。",
            notice="",
            next_url=next_url,
            username=username,
        )

    if is_admin_authenticated():
        return redirect("/admin")
    return render_template_string(
        ADMIN_LOGIN_HTML,
        error="",
        notice=str(getattr(request, "args", {}).get("notice", "") or ""),
        next_url=sanitize_admin_next(getattr(request, "args", {}).get("next")),
        username="",
    )


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_authed", None)
    session.pop("admin_user", None)
    return redirect(url_for("admin_login", notice="已退出后台登录"))


def _run_rebuild_existing_report_html(task_id: str) -> None:
    set_rebuild_state(task_id, "running", "正在重建 HTML/PDF...")
    try:
        rebuild_existing_report_html(task_id, export_pdf=True)
    except Exception as exc:
        set_rebuild_state(task_id, "error", str(exc))
    else:
        set_rebuild_state(task_id, "done", "HTML/PDF 已重建完成")


@app.route("/admin/rebuild-html/<task_id>", methods=["POST"])
def admin_rebuild_html(task_id):
    auth_redirect = require_admin_auth()
    if auth_redirect is not None:
        return auth_redirect
    state = get_rebuild_state(task_id)
    if state["status"] == "running":
        return redirect(url_for("admin_page", notice="该报告正在重建中，请稍后刷新查看。", notice_type="success"))

    thread = threading.Thread(target=_run_rebuild_existing_report_html, args=(task_id,), daemon=True)
    thread.start()
    return redirect(url_for("admin_page", notice="已开始后台重建 HTML，请稍后刷新查看结果。", notice_type="success"))


@app.route("/admin")
def admin_page():
    auth_redirect = require_admin_auth()
    if auth_redirect is not None:
        return auth_redirect
    reconciled_count = reconcile_stale_generation_tasks()
    db_tasks = list_tasks()

    task_list = []
    for row in db_tasks:
        rebuild_state = get_rebuild_state(row.get("task_id", ""))
        task_list.append({
            "id": row.get("task_id", ""),
            "name": row.get("student_name", "未知"),
            "school": row.get("school", "未知"),
            "grade": row.get("grade", "未知"),
            "solved": row.get("solved_count", "-"),
            "failed": row.get("failed_count", "-"),
            "status": row.get("status", "unknown"),
            "time": row.get("eval_time") or row.get("created_at", "-"),
            "html": row.get("html", ""),
            "pdf": _download_report_url(str(row.get("pdf", "") or "")),
            "md": row.get("md", ""),
            "rebuild_status": rebuild_state.get("status", ""),
            "rebuild_message": rebuild_state.get("message", ""),
            "can_rebuild": bool(row.get("status") == "done" and row.get("md")),
            "is_orphan": False,
            "sort_time": _parse_admin_time(row.get("eval_time") or row.get("created_at", "")),
        })

    orphan_tasks = discover_orphan_report_tasks()
    task_list.extend(orphan_tasks)
    task_list.sort(key=lambda task: task.get("sort_time", datetime.min), reverse=True)

    today_prefix = datetime.now().strftime("%Y-%m-%d")
    total_tasks = len(task_list)
    today_tasks = sum(1 for task in task_list if str(task.get("time", "")).startswith(today_prefix))
    error_tasks = sum(1 for task in task_list if str(task.get("status", "")) == "error")
    orphan_count = sum(1 for task in task_list if task.get("is_orphan"))

    return render_template_string(
        ADMIN_HTML,
        total_tasks=total_tasks,
        today_tasks=today_tasks,
        error_tasks=error_tasks,
        tasks=task_list,
        notice=(
            str(request.args.get("notice", "") or "")
            or (
                f"已自动修正 {reconciled_count} 条失真的进行中任务状态。"
                if reconciled_count else
                (f"已补充展示 {orphan_count} 个仅存在于 reports 目录中的历史报告。" if orphan_count else "")
            )
        ),
        notice_type=str(request.args.get("notice_type", "") or "success"),
        admin_user=str(session.get("admin_user", "") or "admin"),
        running_tasks=get_active_generation_task_count(),
    )


# ============================================================
#  v3.5 Phase 1 · 学员档案 admin 路由
#  - /admin/students                       列表
#  - /admin/students/new                   新建表单
#  - /admin/students/<id>                  详情（含 GESP 段位图）
#  - /admin/students/<id>/delete           删除（POST）
#  - /admin/students/<id>/gesp/new         录入 GESP 成绩
# ============================================================
import sqlite3 as _sqlite3
import admin_students as _admin_students
import admin_guardians as _admin_guardians
import admin_goals as _admin_goals
import weekly_reports as _weekly_reports


def _gesp_competition_options() -> list[dict]:
    """拉取所有 GESP 赛事，按日期倒序，供录入 GESP 成绩时选择"""
    conn = _admin_students._get_conn()
    try:
        rows = conn.execute(
            """
            SELECT id, code, name, exam_date
            FROM competitions
            WHERE type = 'gesp'
            ORDER BY exam_date DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.route("/admin/students")
def admin_students_list():
    auth_redirect = require_admin_auth()
    if auth_redirect is not None:
        return auth_redirect
    students = _admin_students.list_students(limit=200)
    total = _admin_students.count_students()
    return render_template_string(
        ADMIN_STUDENTS_LIST_HTML,
        students=students,
        total=total,
        notice=str(request.args.get("notice", "") or ""),
        notice_type=str(request.args.get("notice_type", "") or "success"),
    )


@app.route("/admin/students/new", methods=["GET", "POST"])
def admin_students_new():
    auth_redirect = require_admin_auth()
    if auth_redirect is not None:
        return auth_redirect
    if request.method == "POST":
        try:
            sid = _admin_students.create_student(
                luogu_uid=str(request.form.get("luogu_uid", "")).strip(),
                real_name=str(request.form.get("real_name", "") or "").strip() or None,
                school=str(request.form.get("school", "") or "").strip() or None,
                grade=str(request.form.get("grade", "") or "").strip() or None,
                is_minor=request.form.get("is_minor") == "1",
                note=str(request.form.get("note", "") or "").strip() or None,
            )
            return redirect(
                url_for("admin_students_detail", student_id=sid, notice="学员已创建", notice_type="success")
            )
        except (ValueError, _sqlite3.IntegrityError) as exc:
            return render_template_string(
                ADMIN_STUDENTS_NEW_HTML,
                error=str(exc),
                form=request.form,
            )
    return render_template_string(ADMIN_STUDENTS_NEW_HTML, error="", form={})


@app.route("/admin/students/<int:student_id>")
def admin_students_detail(student_id: int):
    auth_redirect = require_admin_auth()
    if auth_redirect is not None:
        return auth_redirect
    progress = _admin_students.get_student_gesp_progress(student_id)
    if not progress:
        return redirect(
            url_for("admin_students_list", notice=f"学员 {student_id} 不存在", notice_type="error")
        )
    gesp_events = _gesp_competition_options()
    return render_template_string(
        ADMIN_STUDENTS_DETAIL_HTML,
        progress=progress,
        student=progress["student"],
        gesp_events=gesp_events,
        notice=str(request.args.get("notice", "") or ""),
        notice_type=str(request.args.get("notice_type", "") or "success"),
    )


@app.route("/admin/students/<int:student_id>/delete", methods=["POST"])
def admin_students_delete(student_id: int):
    auth_redirect = require_admin_auth()
    if auth_redirect is not None:
        return auth_redirect
    ok = _admin_students.delete_student(student_id)
    if ok:
        return redirect(
            url_for("admin_students_list", notice="学员已删除", notice_type="success")
        )
    return redirect(
        url_for("admin_students_list", notice="学员不存在", notice_type="error")
    )


# ============================================================
# v3.5.2 · 3 版本报告路由
# ============================================================


def _collect_report_data(student: dict) -> dict:
    """共享报告数据生成器（v3.5.2 · 同一份洛谷数据 + AI 分析 → 3 套 UI）"""
    sid = int(student.get("id") or 0)
    progress = _admin_students.get_student_gesp_progress(sid) or {}
    # 错题本（demo 用 stub · 真实从 luogu_evaluator 抽）
    mistake_count = 0
    try:
        from mistake_book import list_mistakes
        mistakes = list_mistakes(sid) or []
        mistake_count = len(mistakes)
    except Exception:
        mistakes = []
    # 政策匹配
    try:
        from task_store import match_school_for_student
        policy_match = match_school_for_student(dict(student))
    except Exception:
        policy_match = {"stage": "unknown", "matches": []}
    # 年龄 & 免初赛
    from docs.gesp_estimator import is_csp_age_eligible, compute_exemptions
    from datetime import date as _date
    gesp_level = int(student.get("gesp_highest_passed") or 0)
    gesp_score = int(student.get("gesp_latest_score") or 0)
    exemptions = compute_exemptions(gesp_level, gesp_score) if gesp_level else []
    return {
        "student": dict(student),
        "progress": progress,
        "mistakes": mistakes,
        "mistake_count": mistake_count,
        "policy_match": policy_match,
        "exemptions": exemptions,
        "gesp_level": gesp_level,
        "gesp_score": gesp_score,
        "next_level": int(student.get("gesp_next_eligible_level") or 1),
        "report_year": 2026,
    }


@app.route("/report/student/<luogu_uid>")
def report_student(luogu_uid: str):
    """v3.5.2 学员版报告（游戏化 · 段位 + 错题本 + AI 讲题）"""
    student = _admin_students.get_student_by_uid(luogu_uid)
    if not student:
        return render_template_string(REGISTER_INVALID_HTML, message=f"UID {luogu_uid} 未注册"), 404
    data = _collect_report_data(student)
    # 检查家长订阅（AI 讲题）
    has_parent_sub = False
    try:
        from task_store import _get_conn
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM activation_codes ac "
                "JOIN students s ON s.id = ac.student_id "
                "WHERE ac.sku = 'parent_sub' AND s.luogu_uid = ? "
                "AND ac.redeemed_at IS NOT NULL "
                "AND (ac.expires_at IS NULL OR ac.expires_at > datetime('now'))",
                (str(luogu_uid).strip(),),
            ).fetchone()
        finally:
            conn.close()
        has_parent_sub = bool(row and dict(row).get("n", 0) > 0)
    except Exception:
        has_parent_sub = False
    return render_template_string(STUDENT_REPORT_HTML, **data, has_parent_sub=has_parent_sub, luogu_uid=luogu_uid)


@app.route("/report/parent/<token>")
def report_parent(token: str):
    """v3.5.2 家长版报告（决策树 + 政策匹配 + 完整）"""
    g = _admin_guardians.get_guardian_by_token(token)
    if not g:
        return render_template_string(REGISTER_INVALID_HTML, message="家长 token 无效或已过期"), 404
    student = _admin_students.get_student(int(g["student_id"]))
    if not student:
        return render_template_string(REGISTER_INVALID_HTML, message="学员已注销"), 404
    data = _collect_report_data(student)
    return render_template_string(PARENT_REPORT_HTML, **data, token=token, guardian=g)


@app.route("/report/coach")
def report_coach():
    """v3.5.2 教练版报告（班级概览 · 复用 admin 数据）"""
    auth_redirect = require_admin_auth()
    if auth_redirect is not None:
        return auth_redirect
    # 班级概览
    students = _admin_students.list_students() or []
    n_students = len(students)
    n_gesp_passed = sum(1 for s in students if int(s.get("gesp_highest_passed") or 0) > 0)
    n_exempt_cspj = sum(1 for s in students if s.get("gesp_can_exempt_csp_j"))
    n_exempt_csps = sum(1 for s in students if s.get("gesp_can_exempt_csp_s"))
    # 营收
    from phase3_dashboard import get_revenue_stats
    rev = get_revenue_stats() or {}
    return render_template_string(
        COACH_REPORT_HTML,
        n_students=n_students,
        n_gesp_passed=n_gesp_passed,
        n_exempt_cspj=n_exempt_cspj,
        n_exempt_csps=n_exempt_csps,
        revenue=rev,
        students=students[:20],  # Top 20
    )


# 学员版报告模板（游戏化）
STUDENT_REPORT_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>🎓 学员版报告 · {{ student.real_name or luogu_uid }}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body{background:linear-gradient(135deg,#fef3c7 0%,#ecfdf5 50%,#dbeafe 100%);min-height:100vh;font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;}
        .card-shadow{box-shadow:0 10px 25px rgba(0,0,0,.06);}
        .progress-fill{transition:width 1s ease;}
        .medal{font-size:36px;display:inline-block;filter:drop-shadow(0 2px 4px rgba(0,0,0,.1));}
    </style>
</head>
<body class="p-4">
<div class="max-w-3xl mx-auto py-6 space-y-4">

    <!-- 头部：欢迎 + 段位大徽章 -->
    <div class="bg-white rounded-2xl card-shadow p-6 text-center">
        <div class="text-sm text-gray-500">🎓 学员版报告</div>
        <h1 class="text-2xl font-extrabold text-gray-800 mt-1">Hi，{{ student.real_name or '选手' }}！</h1>
        <p class="text-xs text-gray-400 mt-1">{{ student.city or '未填城市' }} · {{ student.grade_label or student.grade or '—' }} · UID {{ luogu_uid }}</p>
        <div class="mt-4 flex items-center justify-center gap-4">
            <div>
                <div class="medal">{% if gesp_level >= 7 %}🏆{% elif gesp_level >= 4 %}🏅{% elif gesp_level >= 1 %}⭐{% else %}🌱{% endif %}</div>
                <div class="text-xs text-gray-500 mt-1">当前段位 GESP {{ gesp_level or '0' }} 级</div>
            </div>
            <div class="text-left flex-1 max-w-xs">
                <div class="text-sm font-bold text-emerald-700">{% if gesp_level >= 8 %}已达 8 级 80+ 免 CSP-S 初赛！{% elif gesp_level >= 7 %}7 级 80+ 即可免 CSP-J 初赛{% else %}距离免初赛还差 {{ 7 - gesp_level if gesp_level < 7 else 1 }} 个级别{% endif %}</div>
                <div class="mt-2 w-full bg-gray-200 rounded-full h-2">
                    <div class="bg-emerald-500 h-2 rounded-full progress-fill" style="width: {{ (gesp_level/8*100)|int if gesp_level else 0 }}%"></div>
                </div>
            </div>
        </div>
    </div>

    <!-- 错题本卡片（游戏化） -->
    <div class="bg-white rounded-2xl card-shadow p-5">
        <div class="flex items-center justify-between mb-3">
            <h2 class="text-base font-bold text-gray-800">📚 我的错题本</h2>
            <span class="text-xs text-gray-400">{{ mistake_count }} 道错题</span>
        </div>
        {% if mistakes %}
        <div class="space-y-2">
            {% for m in mistakes[:5] %}
            <div class="flex items-center justify-between border border-gray-200 rounded-lg p-2 hover:bg-gray-50">
                <div class="text-sm">
                    <span class="font-mono text-xs text-gray-400">{{ m.problem_id or m.pid or '—' }}</span>
                    <span class="ml-2">{{ m.title or m.problem_title or '未命名题目' }}</span>
                </div>
                <span class="text-xs px-2 py-0.5 bg-amber-100 text-amber-700 rounded">{{ m.tag or m.algorithm_tag or '未分类' }}</span>
            </div>
            {% endfor %}
        </div>
        {% else %}
        <div class="text-center py-4 text-sm text-gray-400">🌱 暂无错题 · <a href="/me/{{ luogu_uid }}" class="text-emerald-600 hover:underline">查看错题本</a></div>
        {% endif %}
        <!-- AI 讲题按钮（家长订阅门控） -->
        <div class="mt-3 pt-3 border-t border-gray-100">
            {% if has_parent_sub %}
            <a href="/studymate/dashboard" class="block w-full text-center py-2.5 rounded-lg bg-gradient-to-r from-blue-500 to-cyan-500 text-white font-bold text-sm hover:from-blue-600 hover:to-cyan-600">🤖 一键 AI 讲题（StudyMate）</a>
            {% else %}
            <button disabled class="w-full py-2.5 rounded-lg bg-gray-100 text-gray-400 font-bold text-sm cursor-not-allowed">🤖 AI 讲题 🔒（需家长订阅）</button>
            <p class="text-center text-xs text-amber-600 mt-1">💡 让家长加 V 兑换码 <code class="bg-amber-50 px-1 rounded">PS-XXXXXXXX</code> 解锁</p>
            {% endif %}
        </div>
    </div>

    <!-- 下一步行动（游戏化建议） -->
    <div class="bg-white rounded-2xl card-shadow p-5">
        <h2 class="text-base font-bold text-gray-800 mb-3">🎯 下一步行动</h2>
        <div class="space-y-2">
            {% if gesp_level == 0 %}
            <div class="flex items-start gap-3 p-3 bg-emerald-50 rounded-lg">
                <span class="text-2xl">🚀</span>
                <div class="flex-1">
                    <div class="font-bold text-sm text-emerald-800">建议先报 GESP 1 级</div>
                    <div class="text-xs text-emerald-600 mt-0.5">从 1 级开始是硬规则 · 通过后 90+ 可跳级</div>
                </div>
                <a href="/me/{{ luogu_uid }}" class="text-xs text-emerald-700 hover:underline whitespace-nowrap">查看详情 →</a>
            </div>
            {% elif gesp_level < 7 %}
            <div class="flex items-start gap-3 p-3 bg-blue-50 rounded-lg">
                <span class="text-2xl">⏭️</span>
                <div class="flex-1">
                    <div class="font-bold text-sm text-blue-800">下次可报 GESP {{ next_level }} 级</div>
                    <div class="text-xs text-blue-600 mt-0.5">上次 {{ gesp_level }} 级 {{ gesp_score }} 分 · 90+ 可跳级</div>
                </div>
            </div>
            {% else %}
            <div class="flex items-start gap-3 p-3 bg-purple-50 rounded-lg">
                <span class="text-2xl">🏆</span>
                <div class="flex-1">
                    <div class="font-bold text-sm text-purple-800">已解锁免初赛特权</div>
                    <div class="text-xs text-purple-600 mt-0.5">{% if 'csp_s' in exemptions %}CSP-S 免初赛{% else %}CSP-J 免初赛{% endif %}</div>
                </div>
            </div>
            {% endif %}
        </div>
    </div>

    <!-- Tab 切换：学员版 / 家长版 -->
    <div class="bg-white rounded-2xl card-shadow p-3 flex gap-2">
        <a href="/report/student/{{ luogu_uid }}" class="flex-1 text-center py-2 rounded-lg bg-emerald-500 text-white font-bold text-sm">🎓 学员版（当前）</a>
        <a href="/parent" class="flex-1 text-center py-2 rounded-lg bg-gray-100 text-gray-700 font-bold text-sm hover:bg-gray-200">👨‍👩‍👧 家长版</a>
    </div>

    <p class="text-center text-xs text-gray-400">v3.5.2 · 学员版报告 · 同一份数据 3 套渲染</p>
</div>
</body>
</html>
"""


# 家长版报告模板（决策树 + 政策匹配）
PARENT_REPORT_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>👨‍👩‍👧 家长版报告 · {{ student.real_name or '' }}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body{background:linear-gradient(135deg,#fffbeb 0%,#fef3c7 50%,#fde68a 100%);min-height:100vh;font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;}
        .card-shadow{box-shadow:0 10px 25px rgba(0,0,0,.06);}
    </style>
</head>
<body class="p-4">
<div class="max-w-3xl mx-auto py-6 space-y-4">

    <!-- 头部：完整档案 -->
    <div class="bg-white rounded-2xl card-shadow p-6">
        <div class="text-sm text-gray-500">👨‍👩‍👧 家长版报告（完整）</div>
        <h1 class="text-2xl font-extrabold text-gray-800 mt-1">您家孩子 · {{ student.real_name or '—' }}</h1>
        <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-4 text-center">
            <div class="bg-emerald-50 rounded-lg p-2">
                <div class="text-xs text-gray-500">城市</div>
                <div class="font-bold text-sm text-emerald-800">{{ student.city or '—' }}</div>
            </div>
            <div class="bg-blue-50 rounded-lg p-2">
                <div class="text-xs text-gray-500">年级</div>
                <div class="font-bold text-sm text-blue-800">{{ student.grade_label or student.grade or '—' }}</div>
            </div>
            <div class="bg-amber-50 rounded-lg p-2">
                <div class="text-xs text-gray-500">段位</div>
                <div class="font-bold text-sm text-amber-800">GESP {{ gesp_level or '0' }} 级</div>
            </div>
            <div class="bg-purple-50 rounded-lg p-2">
                <div class="text-xs text-gray-500">错题</div>
                <div class="font-bold text-sm text-purple-800">{{ mistake_count }} 道</div>
            </div>
        </div>
    </div>

    <!-- 决策树：3 选项给家长 -->
    <div class="bg-white rounded-2xl card-shadow p-5">
        <h2 class="text-base font-bold text-gray-800 mb-3">🌳 升学路径决策</h2>
        <div class="space-y-2">
            <div class="border {% if 'csp_s' in exemptions %}border-emerald-500 bg-emerald-50{% elif 'csp_j' in exemptions %}border-blue-500 bg-blue-50{% else %}border-gray-200{% endif %} rounded-lg p-3">
                <div class="font-bold text-sm">方案 A · 走竞赛保送 / 强基</div>
                <div class="text-xs text-gray-600 mt-1">GESP {{ gesp_level }} 级 + {{ gesp_score }} 分 · 免初赛：{{ exemptions|join('+') or '暂无' }}</div>
            </div>
            <div class="border border-gray-200 rounded-lg p-3">
                <div class="font-bold text-sm">方案 B · 走高考裸分</div>
                <div class="text-xs text-gray-600 mt-1">以高考为主线 · OI 作为兴趣辅助</div>
            </div>
            <div class="border border-gray-200 rounded-lg p-3">
                <div class="font-bold text-sm">方案 C · 双线并行</div>
                <div class="text-xs text-gray-600 mt-1">高考 + 竞赛 · 适合文化课 590+ 选手</div>
            </div>
        </div>
    </div>

    <!-- 政策匹配（已在 /parent/<token> 完整展示，这里给摘要链接） -->
    <div class="bg-white rounded-2xl card-shadow p-5 border-l-4 border-emerald-500">
        <h2 class="text-base font-bold text-gray-800">🏫 升学路径匹配</h2>
        <p class="text-xs text-gray-500 mt-1">
            当前学段：<strong>{{ policy_match.stage_label or '—' }}</strong>
            · 匹配类型：<strong>{{ policy_match.match_type_label or '无' }}</strong>
            · 匹配到 <strong>{{ policy_match.matches|length }}</strong> 所样板学校
        </p>
        <a href="/parent/{{ token }}" class="inline-block mt-2 text-sm text-emerald-600 hover:underline">查看完整升学匹配 →</a>
    </div>

    <!-- 周报快速入口 -->
    <div class="bg-white rounded-2xl card-shadow p-5">
        <div class="flex items-center justify-between">
            <div>
                <h2 class="text-base font-bold text-gray-800">📅 周报与赛事日历</h2>
                <p class="text-xs text-gray-500 mt-1">每周生成 · GESP / CSP / NOIP 倒计时</p>
            </div>
            <a href="/parent/{{ token }}" class="app-btn app-btn-secondary px-4 py-2 text-sm">进入家长中心</a>
        </div>
    </div>

    <!-- Tab 切换 -->
    <div class="bg-white rounded-2xl card-shadow p-3 flex gap-2">
        <a href="/parent" class="flex-1 text-center py-2 rounded-lg bg-gray-100 text-gray-700 font-bold text-sm hover:bg-gray-200">🎓 学员版</a>
        <a href="/report/parent/{{ token }}" class="flex-1 text-center py-2 rounded-lg bg-amber-500 text-white font-bold text-sm">👨‍👩‍👧 家长版（当前）</a>
    </div>

    <p class="text-center text-xs text-gray-400">v3.5.2 · 家长版报告 · 同一份数据 3 套渲染</p>
</div>
</body>
</html>
"""


# 教练版报告模板（复用 admin 数据 + 看板）
COACH_REPORT_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>🎯 教练版报告 · 信竞 AI 报告 v3.5.2</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body{background:linear-gradient(135deg,#f1f5f9 0%,#e0e7ff 100%);min-height:100vh;font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;}
        .card-shadow{box-shadow:0 10px 25px rgba(0,0,0,.06);}
    </style>
</head>
<body class="p-4">
<div class="max-w-5xl mx-auto py-6 space-y-4">

    <!-- 头部 -->
    <div class="bg-white rounded-2xl card-shadow p-6">
        <div class="flex items-center justify-between">
            <div>
                <div class="text-sm text-gray-500">🎯 教练版报告（班级概览）</div>
                <h1 class="text-2xl font-extrabold text-gray-800 mt-1">教练中心 · 班级看板</h1>
            </div>
            <span class="text-xs px-3 py-1 bg-indigo-100 text-indigo-700 rounded-full">v3.5.2</span>
        </div>
    </div>

    <!-- 5 大指标看板 -->
    <div class="grid grid-cols-2 md:grid-cols-5 gap-3">
        <div class="bg-white rounded-2xl card-shadow p-4 text-center">
            <div class="text-2xl font-extrabold text-emerald-600">{{ n_students }}</div>
            <div class="text-xs text-gray-500 mt-1">班级学员数</div>
        </div>
        <div class="bg-white rounded-2xl card-shadow p-4 text-center">
            <div class="text-2xl font-extrabold text-blue-600">{{ n_gesp_passed }}</div>
            <div class="text-xs text-gray-500 mt-1">通过 GESP 学员</div>
        </div>
        <div class="bg-white rounded-2xl card-shadow p-4 text-center">
            <div class="text-2xl font-extrabold text-amber-600">{{ n_exempt_cspj }}</div>
            <div class="text-xs text-gray-500 mt-1">免 CSP-J 学员</div>
        </div>
        <div class="bg-white rounded-2xl card-shadow p-4 text-center">
            <div class="text-2xl font-extrabold text-purple-600">{{ n_exempt_csps }}</div>
            <div class="text-xs text-gray-500 mt-1">免 CSP-S 学员</div>
        </div>
        <div class="bg-white rounded-2xl card-shadow p-4 text-center">
            <div class="text-2xl font-extrabold text-rose-600">¥{{ revenue.get('total_revenue_cny', 0) if revenue else 0 }}</div>
            <div class="text-xs text-gray-500 mt-1">本期营收</div>
        </div>
    </div>

    <!-- Top 20 学员 -->
    <div class="bg-white rounded-2xl card-shadow p-5">
        <h2 class="text-base font-bold text-gray-800 mb-3">📋 Top 20 学员</h2>
        <div class="overflow-x-auto">
            <table class="w-full text-sm">
                <thead class="text-xs text-gray-500 border-b">
                    <tr>
                        <th class="text-left py-1">#</th>
                        <th class="text-left py-1">姓名</th>
                        <th class="text-left py-1">城市</th>
                        <th class="text-left py-1">年级</th>
                        <th class="text-right py-1">GESP</th>
                        <th class="text-right py-1">CSP-J 免</th>
                        <th class="text-right py-1">CSP-S 免</th>
                    </tr>
                </thead>
                <tbody>
                {% for s in students %}
                    <tr class="border-b border-gray-100 hover:bg-gray-50">
                        <td class="py-1.5 text-gray-400">{{ loop.index }}</td>
                        <td class="py-1.5 font-bold text-gray-800">{{ s.real_name or s.luogu_uid }}</td>
                        <td class="py-1.5 text-gray-600">{{ s.city or '—' }}</td>
                        <td class="py-1.5 text-gray-600">{{ s.grade or '—' }}</td>
                        <td class="py-1.5 text-right font-mono">{{ s.gesp_highest_passed or 0 }}</td>
                        <td class="py-1.5 text-right">{% if s.gesp_can_exempt_csp_j %}✅{% else %}—{% endif %}</td>
                        <td class="py-1.5 text-right">{% if s.gesp_can_exempt_csp_s %}✅{% else %}—{% endif %}</td>
                    </tr>
                {% endfor %}
                </tbody>
            </table>
        </div>
    </div>

    <!-- 操作入口 -->
    <div class="bg-white rounded-2xl card-shadow p-5">
        <h2 class="text-base font-bold text-gray-800 mb-3">🔧 操作入口</h2>
        <div class="grid grid-cols-2 md:grid-cols-4 gap-2">
            <a href="/admin/students" class="block text-center py-2 rounded-lg bg-emerald-50 text-emerald-700 text-sm font-bold hover:bg-emerald-100">学员管理</a>
            <a href="/admin/revenue" class="block text-center py-2 rounded-lg bg-amber-50 text-amber-700 text-sm font-bold hover:bg-amber-100">营收看板</a>
            <a href="/redeem" class="block text-center py-2 rounded-lg bg-blue-50 text-blue-700 text-sm font-bold hover:bg-blue-100">兑换码生成</a>
            <a href="/admin/students/new" class="block text-center py-2 rounded-lg bg-purple-50 text-purple-700 text-sm font-bold hover:bg-purple-100">新增学员</a>
        </div>
    </div>

    <p class="text-center text-xs text-gray-400">v3.5.2 · 教练版报告 · 复用 admin 数据</p>
</div>
</body>
</html>
"""


@app.route("/admin/students/<int:student_id>/gesp/new", methods=["GET", "POST"])
def admin_students_gesp_new(student_id: int):
    auth_redirect = require_admin_auth()
    if auth_redirect is not None:
        return auth_redirect
    student = _admin_students.get_student(student_id)
    if not student:
        return redirect(
            url_for("admin_students_list", notice="学员不存在", notice_type="error")
        )
    gesp_events = _gesp_competition_options()

    if request.method == "POST":
        try:
            exam_id = int(request.form.get("exam_id", "0") or "0")
            level = int(request.form.get("registered_level", "0") or "0")
            score = int(request.form.get("actual_score", "-1") or "-1")
            _admin_students.add_gesp_exam(
                student_id=student_id,
                exam_id=exam_id,
                registered_level=level,
                actual_score=score,
                certificate_no=str(request.form.get("certificate_no", "") or "").strip() or None,
                notes=str(request.form.get("notes", "") or "").strip() or None,
                recorded_by="admin",
            )
            return redirect(
                url_for(
                    "admin_students_detail",
                    student_id=student_id,
                    notice=f"已录入 GESP {level} 级 {score} 分",
                    notice_type="success",
                )
            )
        except (ValueError, _sqlite3.IntegrityError) as exc:
            return render_template_string(
                ADMIN_STUDENTS_GESP_NEW_HTML,
                student=student,
                gesp_events=gesp_events,
                error=str(exc),
                form=request.form,
            )
    return render_template_string(
        ADMIN_STUDENTS_GESP_NEW_HTML,
        student=student,
        gesp_events=gesp_events,
        error="",
        form={},
    )


# ---- 学员档案 admin 模板 ----

ADMIN_STUDENTS_LIST_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>学员档案 - 后台管理</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 min-h-screen p-6">
    <div class="max-w-6xl mx-auto">
        <div class="flex items-center justify-between mb-6">
            <h1 class="text-3xl font-bold text-blue-900">学员档案</h1>
            <div class="flex items-center gap-4">
                <a href="/admin/students/new" class="bg-blue-600 text-white px-4 py-2 rounded-lg hover:bg-blue-700">+ 新建学员</a>
                <a href="/admin" class="text-blue-600 hover:underline">返回后台</a>
            </div>
        </div>
        {% if notice %}
        <div class="mb-4 rounded-lg border px-4 py-3 text-sm {% if notice_type == 'error' %}bg-red-50 border-red-200 text-red-700{% else %}bg-green-50 border-green-200 text-green-700{% endif %}">
            {{ notice }}
        </div>
        {% endif %}
        <div class="bg-white rounded-xl shadow overflow-hidden">
            <div class="px-6 py-4 border-b border-gray-200 flex items-center justify-between">
                <h2 class="text-lg font-semibold text-gray-800">学员列表（合计 {{ total }}）</h2>
            </div>
            {% if students %}
            <div class="overflow-x-auto">
                <table class="min-w-full text-sm text-left">
                    <thead class="bg-gray-50 text-gray-600 font-medium">
                        <tr>
                            <th class="px-6 py-3">ID</th>
                            <th class="px-6 py-3">Luogu UID</th>
                            <th class="px-6 py-3">姓名/代号</th>
                            <th class="px-6 py-3">学校</th>
                            <th class="px-6 py-3">GESP 最高</th>
                            <th class="px-6 py-3">免初赛</th>
                            <th class="px-6 py-3">下次可报</th>
                            <th class="px-6 py-3">考试次数</th>
                            <th class="px-6 py-3">操作</th>
                        </tr>
                    </thead>
                    <tbody>
                    {% for s in students %}
                        <tr class="border-t border-gray-100 hover:bg-gray-50">
                            <td class="px-6 py-3 text-gray-500">#{{ s.id }}</td>
                            <td class="px-6 py-3 font-mono text-xs">{{ s.luogu_uid }}</td>
                            <td class="px-6 py-3">{{ s.real_name or ('UID-' + s.luogu_uid) }}{% if s.is_minor %} <span class="text-xs text-orange-500">(未成年)</span>{% endif %}</td>
                            <td class="px-6 py-3 text-gray-600">{{ s.school or '—' }}</td>
                            <td class="px-6 py-3 font-semibold text-blue-700">{% if s.gesp_highest_passed %}GESP {{ s.gesp_highest_passed }} 级{% else %}—{% endif %}</td>
                            <td class="px-6 py-3">
                                {% if s.gesp_can_exempt_csp_s %}<span class="px-2 py-1 text-xs bg-purple-100 text-purple-700 rounded">J+S 免</span>
                                {% elif s.gesp_can_exempt_csp_j %}<span class="px-2 py-1 text-xs bg-green-100 text-green-700 rounded">J 免</span>
                                {% else %}<span class="text-gray-400">—</span>{% endif %}
                            </td>
                            <td class="px-6 py-3 text-gray-700">{% if s.gesp_next_eligible_level %}GESP {{ s.gesp_next_eligible_level }} 级{% else %}GESP 1 级{% endif %}</td>
                            <td class="px-6 py-3 text-gray-500">{{ s.gesp_exam_count }}</td>
                            <td class="px-6 py-3">
                                <a href="/admin/students/{{ s.id }}" class="text-blue-600 hover:underline mr-3">详情</a>
                                <form method="POST" action="/admin/students/{{ s.id }}/delete" class="inline" onsubmit="return confirm('确认删除学员 #{{ s.id }}？将级联删除所有 GESP 记录。');">
                                    <button type="submit" class="text-red-600 hover:underline">删除</button>
                                </form>
                            </td>
                        </tr>
                    {% endfor %}
                    </tbody>
                </table>
            </div>
            {% else %}
            <div class="px-6 py-12 text-center text-gray-500">
                <p class="mb-3">还没有学员。</p>
                <a href="/admin/students/new" class="text-blue-600 hover:underline">立即创建第一个学员</a>
            </div>
            {% endif %}
        </div>
    </div>
</body>
</html>
"""


ADMIN_STUDENTS_NEW_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>新建学员 - 后台管理</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 min-h-screen p-6">
    <div class="max-w-2xl mx-auto">
        <div class="flex items-center justify-between mb-6">
            <h1 class="text-3xl font-bold text-blue-900">新建学员</h1>
            <a href="/admin/students" class="text-blue-600 hover:underline">返回列表</a>
        </div>
        {% if error %}
        <div class="mb-4 rounded-lg border bg-red-50 border-red-200 text-red-700 px-4 py-3 text-sm">
            {{ error }}
        </div>
        {% endif %}
        <form method="POST" class="bg-white rounded-xl shadow p-6 space-y-4">
            <div>
                <label class="block text-sm font-medium text-gray-700 mb-1">Luogu UID <span class="text-red-500">*</span></label>
                <input type="text" name="luogu_uid" required pattern="[0-9]+"
                       value="{{ form.luogu_uid or '' }}"
                       class="w-full border rounded-lg px-3 py-2 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                       placeholder="洛谷用户 ID（数字）">
            </div>
            <div class="grid grid-cols-2 gap-4">
                <div>
                    <label class="block text-sm font-medium text-gray-700 mb-1">真实姓名</label>
                    <input type="text" name="real_name"
                           value="{{ form.real_name or '' }}"
                           class="w-full border rounded-lg px-3 py-2"
                           placeholder="未成年学员需家长授权后才填">
                </div>
                <div>
                    <label class="block text-sm font-medium text-gray-700 mb-1">学校</label>
                    <input type="text" name="school"
                           value="{{ form.school or '' }}"
                           class="w-full border rounded-lg px-3 py-2">
                </div>
            </div>
            <div class="grid grid-cols-2 gap-4">
                <div>
                    <label class="block text-sm font-medium text-gray-700 mb-1">入学年份（年级）</label>
                    <input type="text" name="grade"
                           value="{{ form.grade or '' }}"
                           placeholder="如：2024"
                           class="w-full border rounded-lg px-3 py-2">
                </div>
                <div>
                    <label class="block text-sm font-medium text-gray-700 mb-1">是否未成年（&lt;14 岁）</label>
                    <select name="is_minor" class="w-full border rounded-lg px-3 py-2">
                        <option value="0" {% if form.is_minor != '1' %}selected{% endif %}>否</option>
                        <option value="1" {% if form.is_minor == '1' %}selected{% endif %}>是</option>
                    </select>
                    <p class="mt-1 text-xs text-gray-500">选「是」则姓名/学校强制留空至家长授权</p>
                </div>
            </div>
            <div>
                <label class="block text-sm font-medium text-gray-700 mb-1">备注</label>
                <textarea name="note" rows="3" class="w-full border rounded-lg px-3 py-2">{{ form.note or '' }}</textarea>
            </div>
            <div class="flex gap-3 pt-2">
                <button type="submit" class="bg-blue-600 text-white px-6 py-2 rounded-lg hover:bg-blue-700">创建学员</button>
                <a href="/admin/students" class="px-6 py-2 rounded-lg border border-gray-300 text-gray-700 hover:bg-gray-50">取消</a>
            </div>
        </form>
    </div>
</body>
</html>
"""


ADMIN_STUDENTS_DETAIL_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>学员详情 - 后台管理</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 min-h-screen p-6">
    <div class="max-w-5xl mx-auto">
        <div class="flex items-center justify-between mb-6">
            <h1 class="text-3xl font-bold text-blue-900">学员 #{{ student.id }} {{ student.real_name or ('UID-' + student.luogu_uid) }}</h1>
            <div class="flex items-center gap-4">
                <a href="/admin/students/{{ student.id }}/gesp/new" class="bg-blue-600 text-white px-4 py-2 rounded-lg hover:bg-blue-700">+ 录入 GESP 成绩</a>
                <a href="/admin/students" class="text-blue-600 hover:underline">返回列表</a>
            </div>
        </div>
        {% if notice %}
        <div class="mb-4 rounded-lg border px-4 py-3 text-sm {% if notice_type == 'error' %}bg-red-50 border-red-200 text-red-700{% else %}bg-green-50 border-green-200 text-green-700{% endif %}">
            {{ notice }}
        </div>
        {% endif %}
        <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <!-- 基本信息 -->
            <div class="bg-white rounded-xl shadow p-6">
                <h2 class="text-lg font-semibold text-gray-800 mb-4">基本信息</h2>
                <dl class="space-y-2 text-sm">
                    <div class="flex"><dt class="w-24 text-gray-500">Luogu UID</dt><dd class="font-mono">{{ student.luogu_uid }}</dd></div>
                    <div class="flex"><dt class="w-24 text-gray-500">姓名</dt><dd>{{ student.real_name or '— 未填（未授权或已脱敏）—' }}</dd></div>
                    <div class="flex"><dt class="w-24 text-gray-500">学校</dt><dd>{{ student.school or '—' }}</dd></div>
                    <div class="flex"><dt class="w-24 text-gray-500">年级</dt><dd>{{ student.grade or '—' }}</dd></div>
                    <div class="flex"><dt class="w-24 text-gray-500">未成年</dt><dd>{% if student.is_minor %}是{% else %}否{% endif %}</dd></div>
                    <div class="flex"><dt class="w-24 text-gray-500">家长授权</dt><dd>{{ student.guardian_consent_at or '未授权' }}</dd></div>
                    <div class="flex"><dt class="w-24 text-gray-500">备注</dt><dd>{{ student.note or '—' }}</dd></div>
                </dl>
            </div>
            <!-- GESP 状态 -->
            <div class="bg-white rounded-xl shadow p-6">
                <h2 class="text-lg font-semibold text-gray-800 mb-4">GESP 状态</h2>
                <div class="text-center font-mono text-lg tracking-wide mb-4 text-gray-700">
                    {{ progress.progress_bar }}
                </div>
                <dl class="space-y-2 text-sm">
                    <div class="flex"><dt class="w-32 text-gray-500">最高已过</dt><dd class="font-semibold text-blue-700">{% if progress.passed_levels %}GESP {{ progress.passed_levels[-1] }} 级{% else %}无{% endif %}</dd></div>
                    <div class="flex"><dt class="w-32 text-gray-500">下次可报</dt><dd class="font-semibold text-green-700">GESP {{ progress.next_eligible_level }} 级</dd></div>
                    <div class="flex"><dt class="w-32 text-gray-500">免初赛状态</dt><dd>
                        {% if progress.can_exempt_csp_s %}<span class="px-2 py-1 text-xs bg-purple-100 text-purple-700 rounded">CSP-J + CSP-S 双免</span>
                        {% elif progress.can_exempt_csp_j %}<span class="px-2 py-1 text-xs bg-green-100 text-green-700 rounded">CSP-J 免</span>
                        {% else %}<span class="text-gray-400">—</span>{% endif %}
                    </dd></div>
                    <div class="flex"><dt class="w-32 text-gray-500">免初赛有效期</dt><dd>{{ progress.exemption_expiry or '—' }}</dd></div>
                </dl>
            </div>
            <!-- 快捷操作 -->
            <div class="bg-white rounded-xl shadow p-6">
                <h2 class="text-lg font-semibold text-gray-800 mb-4">快捷操作</h2>
                <ul class="space-y-2 text-sm">
                    <li><a href="/admin/students/{{ student.id }}/gesp/new" class="text-blue-600 hover:underline">+ 录入新一次 GESP 成绩</a></li>
                    <li><a href="/admin/students" class="text-blue-600 hover:underline">← 返回学员列表</a></li>
                    <li>
                        <form method="POST" action="/admin/students/{{ student.id }}/delete" onsubmit="return confirm('确认删除学员 #{{ student.id }}？将级联删除所有 GESP 记录。');">
                            <button type="submit" class="text-red-600 hover:underline">⚠ 删除学员</button>
                        </form>
                    </li>
                </ul>
            </div>
        </div>
        <!-- GESP 考试历史 -->
        <div class="mt-6 bg-white rounded-xl shadow overflow-hidden">
            <div class="px-6 py-4 border-b border-gray-200">
                <h2 class="text-lg font-semibold text-gray-800">GESP 考试历史（{{ progress.exams|length }} 次）</h2>
            </div>
            {% if progress.exams %}
            <div class="overflow-x-auto">
                <table class="min-w-full text-sm text-left">
                    <thead class="bg-gray-50 text-gray-600 font-medium">
                        <tr>
                            <th class="px-6 py-3">考试日期</th>
                            <th class="px-6 py-3">赛事</th>
                            <th class="px-6 py-3">报 N 级</th>
                            <th class="px-6 py-3">分数</th>
                            <th class="px-6 py-3">通过</th>
                            <th class="px-6 py-3">跳级</th>
                            <th class="px-6 py-3">免初赛</th>
                            <th class="px-6 py-3">证书号</th>
                        </tr>
                    </thead>
                    <tbody>
                    {% for e in progress.exams %}
                        <tr class="border-t border-gray-100">
                            <td class="px-6 py-3 text-gray-600">{{ e.exam_date or '—' }}</td>
                            <td class="px-6 py-3">{{ e.exam_name or e.exam_code or '—' }}</td>
                            <td class="px-6 py-3 font-semibold">{{ e.registered_level }}</td>
                            <td class="px-6 py-3 text-lg font-bold {% if e.actual_score >= 80 %}text-purple-700{% elif e.actual_score >= 60 %}text-green-700{% else %}text-red-600{% endif %}">{{ e.actual_score }}</td>
                            <td class="px-6 py-3">{% if e.passed %}✅{% else %}❌{% endif %}</td>
                            <td class="px-6 py-3">{% if e.can_skip_next %}⚡ 可跳{% else %}—{% endif %}</td>
                            <td class="px-6 py-3">
                                {% if e.exempts_csp_s %}<span class="text-xs bg-purple-100 text-purple-700 px-1.5 py-0.5 rounded">J+S</span>
                                {% elif e.exempts_csp_j %}<span class="text-xs bg-green-100 text-green-700 px-1.5 py-0.5 rounded">J</span>
                                {% else %}<span class="text-gray-400">—</span>{% endif %}
                            </td>
                            <td class="px-6 py-3 font-mono text-xs text-gray-500">{{ e.certificate_no or '—' }}</td>
                        </tr>
                    {% endfor %}
                    </tbody>
                </table>
            </div>
            {% else %}
            <div class="px-6 py-12 text-center text-gray-500">
                <p>暂无 GESP 真考记录。</p>
                <a href="/admin/students/{{ student.id }}/gesp/new" class="text-blue-600 hover:underline">录入第一次 GESP 成绩</a>
            </div>
            {% endif %}
        </div>
    </div>
</body>
</html>
"""


ADMIN_STUDENTS_GESP_NEW_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>录入 GESP 成绩 - 后台管理</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 min-h-screen p-6">
    <div class="max-w-2xl mx-auto">
        <div class="flex items-center justify-between mb-6">
            <h1 class="text-3xl font-bold text-blue-900">录入 GESP 成绩</h1>
            <a href="/admin/students/{{ student.id }}" class="text-blue-600 hover:underline">返回学员详情</a>
        </div>
        <p class="text-sm text-gray-500 mb-4">学员：<span class="font-mono">{{ student.real_name or ('UID-' + student.luogu_uid) }}</span></p>
        {% if error %}
        <div class="mb-4 rounded-lg border bg-red-50 border-red-200 text-red-700 px-4 py-3 text-sm">
            {{ error }}
        </div>
        {% endif %}
        <form method="POST" class="bg-white rounded-xl shadow p-6 space-y-4">
            <div>
                <label class="block text-sm font-medium text-gray-700 mb-1">GESP 考试场次 <span class="text-red-500">*</span></label>
                <select name="exam_id" required class="w-full border rounded-lg px-3 py-2">
                    <option value="">-- 选择考试 --</option>
                    {% for e in gesp_events %}
                    <option value="{{ e.id }}" {% if form.exam_id|string == e.id|string %}selected{% endif %}>
                        {{ e.exam_date }}  {{ e.name }}  ({{ e.code }})
                    </option>
                    {% endfor %}
                </select>
            </div>
            <div class="grid grid-cols-2 gap-4">
                <div>
                    <label class="block text-sm font-medium text-gray-700 mb-1">报 N 级 <span class="text-red-500">*</span></label>
                    <select name="registered_level" required class="w-full border rounded-lg px-3 py-2">
                        {% for n in range(1, 9) %}
                        <option value="{{ n }}" {% if form.registered_level|string == n|string %}selected{% endif %}>GESP {{ n }} 级</option>
                        {% endfor %}
                    </select>
                </div>
                <div>
                    <label class="block text-sm font-medium text-gray-700 mb-1">实际分数 <span class="text-red-500">*</span></label>
                    <input type="number" name="actual_score" required min="0" max="100"
                           value="{{ form.actual_score or '' }}"
                           class="w-full border rounded-lg px-3 py-2"
                           placeholder="0-100">
                    <p class="mt-1 text-xs text-gray-500">
                        ≥90 触发跳级 / ≥80 触发免初赛（CSP-J 7/8 级，CSP-S 8 级）
                    </p>
                </div>
            </div>
            <div>
                <label class="block text-sm font-medium text-gray-700 mb-1">证书编号</label>
                <input type="text" name="certificate_no" value="{{ form.certificate_no or '' }}"
                       class="w-full border rounded-lg px-3 py-2"
                       placeholder="GESP-YYYY-NN-XXXXX（可选）">
            </div>
            <div>
                <label class="block text-sm font-medium text-gray-700 mb-1">教练备注</label>
                <textarea name="notes" rows="2" class="w-full border rounded-lg px-3 py-2">{{ form.notes or '' }}</textarea>
            </div>
            <div class="flex gap-3 pt-2">
                <button type="submit" class="bg-blue-600 text-white px-6 py-2 rounded-lg hover:bg-blue-700">保存成绩</button>
                <a href="/admin/students/{{ student.id }}" class="px-6 py-2 rounded-lg border border-gray-300 text-gray-700 hover:bg-gray-50">取消</a>
            </div>
        </form>
    </div>
</body>
</html>
"""


# ============================================================
# v3.5.2 · 兑换码激活（/redeem）+ 教练版咨询（/coach）
# ============================================================


# ============================================================
# v3.5.2 · 统一报告生成入口（v1 模式 + 注册信息融合）
# ============================================================


@app.route("/generate-form", methods=["GET"])
def generate_form():
    """v3.5.2 统一报告生成表单（融合 v1 报告 + v3.5.2 注册字段）

    一次性填写：洛谷 Cookies + 报告信息（UID/姓名/学校/年级/城市）+
    选手信息（性别/出生日期/手机）+ OpenAI 配置 + PIPL 同意
    """
    return render_template_string(GENERATE_FORM_HTML, form={}, server_key_hint=_get_server_key_hint())


@app.route("/generate-form", methods=["POST"])
def generate_form_submit():
    """POST：先注册（如未注册） → 同步创建 students + 报告 → 跳 /me/<uid>"""
    import re as _re
    form = request.form.to_dict()

    # 必填校验
    required = ["client_id", "uid", "c3vk", "real_name", "city", "grade"]
    missing = [k for k in required if not (form.get(k) or "").strip()]
    if missing:
        return render_template_string(
            GENERATE_FORM_HTML,
            form=form,
            server_key_hint=_get_server_key_hint(),
            error=f"请填写必填项：{', '.join(missing)}",
        ), 400

    # UID 格式
    luogu_uid = (form.get("uid") or "").strip()
    if not _re.match(r"^\d{6,10}$", luogu_uid):
        return render_template_string(
            GENERATE_FORM_HTML,
            form=form,
            server_key_hint=_get_server_key_hint(),
            error="UID 必须是 6-10 位数字",
        ), 400

    # PIPL 同意
    if not form.get("agree"):
        return render_template_string(
            GENERATE_FORM_HTML,
            form=form,
            server_key_hint=_get_server_key_hint(),
            error="请先同意《个人信息处理规则》（PIPL）",
        ), 400

    # 1) 注册或更新学生档案
    try:
        existing = _admin_students.get_student_by_uid(luogu_uid)
        if existing:
            sid = int(existing["id"])
            # 直接 SQL 更新（students 表字段：real_name/city/grade/gender/school/birth_date）
            from task_store import _get_conn
            conn = _get_conn()
            try:
                conn.execute(
                    """
                    UPDATE students SET
                        real_name = ?, city = ?, grade = ?, gender = ?, school = ?, birth_date = ?
                    WHERE id = ?
                    """,
                    (
                        (form.get("real_name") or "").strip() or None,
                        (form.get("city") or "").strip() or None,
                        (form.get("grade") or "").strip() or None,
                        (form.get("gender") or "").strip() or None,
                        (form.get("school") or "").strip() or None,
                        (form.get("birth_date") or "").strip() or None,
                        sid,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        else:
            new_sid = _admin_students.create_student(
                luogu_uid=luogu_uid,
                real_name=(form.get("real_name") or "").strip(),
                city=(form.get("city") or "").strip(),
                grade=(form.get("grade") or "").strip(),
                gender=(form.get("gender") or "").strip() or None,
                school=(form.get("school") or "").strip() or None,
                birth_date=(form.get("birth_date") or "").strip() or None,
                registered_via="generate_form",
            )
            sid = int(new_sid)
            # 若有手机号，存到 guardians 表（v3.5.2 支持）
            phone = (form.get("phone") or "").strip()
            if phone and re.match(r"^1[3-9]\d{9}$", phone):
                try:
                    from admin_guardians import upsert_guardian_by_phone
                    upsert_guardian_by_phone(sid, phone, display_name=form.get("real_name") or "学员")
                except Exception:
                    pass  # 失败不阻塞主流程
    except Exception as e:
        return render_template_string(
            GENERATE_FORM_HTML,
            form=form,
            server_key_hint=_get_server_key_hint(),
            error=f"注册失败：{e}",
        ), 500

    # 2) 触发 v1 报告生成（复用现有 run_generation → 跳 /status/<task_id> 看进度）
    try:
        import json as _json
        task_id = str(uuid.uuid4())
        # 把 v3.5.2 表单字段映射到 v1 form_data 字段
        v1_form = {
            "client_id": form.get("client_id", ""),
            "uid": luogu_uid,
            "c3vk": form.get("c3vk", ""),
            "api_key": form.get("api_key", ""),
            "base_url": form.get("base_url", ""),
            "model_name": form.get("model_name", ""),
            "student_name": (form.get("real_name") or "").strip(),
            "school": (form.get("school") or "").strip(),
            "grade": (form.get("grade") or "").strip(),
        }
        with TASKS_LOCK:
            insert_task(task_id, status="queued", message="排队中...")
            update_task(
                task_id,
                student_name=v1_form["student_name"] or "未知选手",
                school=v1_form["school"] or "未知学校",
                grade=v1_form["grade"] or "未知年级",
                retry_form_json=_json.dumps(build_retry_form_snapshot(v1_form), ensure_ascii=False),
            )
        thread = threading.Thread(target=run_generation, args=(task_id, v1_form), daemon=True)
        register_active_generation_task(task_id, thread)
        thread.start()
        # 跳到 /status/<task_id>，完成后可手动跳到 /me/<uid>
        return redirect(url_for("status_page", task_id=task_id) + f"?luogu_uid={luogu_uid}")
    except Exception as e:
        # 即使报告生成失败，也跳到 me（注册已完成）
        return redirect(url_for("student_me", luogu_uid=luogu_uid))


# 任务 cookies 暂存（v1 报告生成需要的 cookies）
_TASK_COOKIES: dict[str, dict] = {}


# 统一报告生成表单模板（v3.5.2 · 融合 v1 + 注册字段）
GENERATE_FORM_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>🎓 AI 生成学习报告 · 信竞 AI 报告 v3.5.2</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body{background:linear-gradient(135deg,#ecfdf5 0%,#f0f9ff 100%);min-height:100vh;font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;}
        .card-shadow{box-shadow:0 10px 25px rgba(0,0,0,.06);}
        .field-section{border-left:4px solid #059669;padding-left:12px;margin-bottom:16px;}
        .field-section h3{font-size:14px;font-weight:800;color:#047857;margin-bottom:8px;}
        .app-input,.app-select{width:100%;border:1px solid #d1d5db;border-radius:8px;padding:8px 10px;font-size:14px;transition:all .15s ease;}
        .app-input:focus,.app-select:focus{outline:none;border-color:#10b981;box-shadow:0 0 0 3px rgba(16,185,129,.15);}
        .app-label{font-size:12px;font-weight:600;color:#374151;}
        .app-btn{width:100%;font-weight:800;border-radius:10px;padding:12px 16px;transition:all .15s ease;cursor:pointer;font-size:15px;}
        .app-btn-primary{background:linear-gradient(135deg,#059669 0%,#0d9488 100%);color:#fff;border:none;}
        .app-btn-primary:hover{transform:translateY(-1px);box-shadow:0 8px 20px rgba(5,150,105,.3);}
    </style>
</head>
<body class="p-4">
<div class="max-w-2xl mx-auto py-6 space-y-4">

    <div class="text-center mb-4">
        <span class="inline-block px-3 py-1 bg-emerald-100 text-emerald-700 text-xs rounded-full">🎯 v3.5.2 统一入口</span>
        <h1 class="text-2xl font-extrabold text-gray-800 mt-2">🎓 AI 生成学习报告</h1>
        <p class="text-sm text-gray-500 mt-1">一次性填写 · 30 秒出报告 · 3 版本报告 + 错题本 + 段位</p>
    </div>

    {% if error %}
    <div class="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-700">{{ error }}</div>
    {% endif %}

    <form action="/generate-form" method="post" class="bg-white rounded-2xl card-shadow p-6 space-y-5">

        <div class="bg-amber-50 border border-amber-200 rounded-lg p-3 text-xs text-amber-800">
            ℹ️ <strong>如何获取洛谷 Cookies</strong>：登录 <code>luogu.com.cn</code> → F12 → Application → Cookies → 复制 <code>__client_id</code> / <code>_uid</code> / <code>C3VK</code>
        </div>

        <!-- 1. 洛谷账号 -->
        <div class="field-section">
            <h3>📡 1. 洛谷账号（必填 · 用于抓取做题数据）</h3>
            <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <div>
                    <label class="app-label">__client_id</label>
                    <input type="text" name="client_id" required value="{{ form.get('client_id','') }}" class="app-input mt-1" placeholder="32 位">
                </div>
                <div>
                    <label class="app-label">_uid</label>
                    <input type="text" name="uid" required pattern="\\d{6,10}" value="{{ form.get('uid','') }}" class="app-input mt-1" placeholder="6-10 位">
                </div>
                <div>
                    <label class="app-label">C3VK</label>
                    <input type="text" name="c3vk" required value="{{ form.get('c3vk','') }}" class="app-input mt-1" placeholder="token">
                </div>
            </div>
        </div>

        <!-- 2. 报告核心信息（融合注册字段） -->
        <div class="field-section">
            <h3>👤 2. 报告信息（必填 · 报告核心数据 + 注册字段）</h3>
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                    <label class="app-label">真实姓名 <span class="text-red-500">*</span></label>
                    <input type="text" name="real_name" required maxlength="20" value="{{ form.get('real_name','') }}" class="app-input mt-1" placeholder="用于报告抬头">
                </div>
                <div>
                    <label class="app-label">学校 <span class="text-red-500">*</span></label>
                    <input type="text" name="school" value="{{ form.get('school','') }}" class="app-input mt-1" placeholder="如：人大附中早培班">
                </div>
            </div>
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 mt-3">
                <div>
                    <label class="app-label">所在城市 <span class="text-red-500">*</span></label>
                    <input type="text" name="city" required value="{{ form.get('city','') }}" class="app-input mt-1" placeholder="如：杭州（用于本地科技特长生匹配）">
                </div>
                <div>
                    <label class="app-label">年级 <span class="text-red-500">*</span></label>
                    <select name="grade" required class="app-select mt-1">
                        <option value="">请选择年级</option>
                        <optgroup label="小学">
                            <option value="PRIMARY_1" {% if form.get('grade') == 'PRIMARY_1' %}selected{% endif %}>一年级</option>
                            <option value="PRIMARY_2" {% if form.get('grade') == 'PRIMARY_2' %}selected{% endif %}>二年级</option>
                            <option value="PRIMARY_3" {% if form.get('grade') == 'PRIMARY_3' %}selected{% endif %}>三年级</option>
                            <option value="PRIMARY_4" {% if form.get('grade') == 'PRIMARY_4' %}selected{% endif %}>四年级</option>
                            <option value="PRIMARY_5" {% if form.get('grade') == 'PRIMARY_5' %}selected{% endif %}>五年级</option>
                            <option value="PRIMARY_6" {% if form.get('grade') == 'PRIMARY_6' %}selected{% endif %}>六年级</option>
                        </optgroup>
                        <optgroup label="初中">
                            <option value="JUNIOR_1" {% if form.get('grade') == 'JUNIOR_1' %}selected{% endif %}>初一</option>
                            <option value="JUNIOR_2" {% if form.get('grade') == 'JUNIOR_2' %}selected{% endif %}>初二</option>
                            <option value="JUNIOR_3" {% if form.get('grade') == 'JUNIOR_3' %}selected{% endif %}>初三</option>
                        </optgroup>
                        <optgroup label="高中">
                            <option value="SENIOR_1" {% if form.get('grade') == 'SENIOR_1' %}selected{% endif %}>高一</option>
                            <option value="SENIOR_2" {% if form.get('grade') == 'SENIOR_2' %}selected{% endif %}>高二</option>
                            <option value="SENIOR_3" {% if form.get('grade') == 'SENIOR_3' %}selected{% endif %}>高三</option>
                        </optgroup>
                        <optgroup label="大学">
                            <option value="UNI_1" {% if form.get('grade') == 'UNI_1' %}selected{% endif %}>大一</option>
                            <option value="UNI_2" {% if form.get('grade') == 'UNI_2' %}selected{% endif %}>大二</option>
                            <option value="UNI_3" {% if form.get('grade') == 'UNI_3' %}selected{% endif %}>大三</option>
                            <option value="UNI_4" {% if form.get('grade') == 'UNI_4' %}selected{% endif %}>大四</option>
                        </optgroup>
                    </select>
                </div>
            </div>
        </div>

        <!-- 3. 选手信息（可选 · 用于 GESP 免初赛判断） -->
        <details class="field-section">
            <summary class="cursor-pointer text-sm font-bold text-gray-600 hover:text-emerald-600">🎂 3. 选手信息（可选 · 用于 GESP 免初赛判断，不填也能生成报告）</summary>
            <div class="grid grid-cols-1 sm:grid-cols-3 gap-3 mt-3">
                <div>
                    <label class="app-label">性别</label>
                    <select name="gender" class="app-select mt-1">
                        <option value="">未填</option>
                        <option value="M" {% if form.get('gender') == 'M' %}selected{% endif %}>男</option>
                        <option value="F" {% if form.get('gender') == 'F' %}selected{% endif %}>女</option>
                    </select>
                </div>
                <div>
                    <label class="app-label">出生日期</label>
                    <input type="date" name="birth_date" value="{{ form.get('birth_date','') }}" class="app-input mt-1">
                </div>
                <div>
                    <label class="app-label">手机号（家长/学员）</label>
                    <input type="tel" name="phone" pattern="1[3-9][0-9]{9}" value="{{ form.get('phone','') }}" class="app-input mt-1" placeholder="11 位（可选）">
                </div>
            </div>
            <p class="text-xs text-gray-500 mt-2">ℹ️ 出生日期用于精确计算 CSP 报名年龄（9 月 1 日前满 12 岁）</p>
        </details>

        <!-- 4. OpenAI 配置（可选） -->
        <details class="field-section">
            <summary class="cursor-pointer text-sm font-bold text-gray-600 hover:text-emerald-600">🤖 4. OpenAI 配置（可选 · 留空使用服务端默认）</summary>
            <div class="space-y-2 mt-3">
                <div>
                    <label class="app-label">API Key</label>
                    <input type="password" name="api_key" value="{{ form.get('api_key','') }}" class="app-input mt-1" placeholder="sk-...">
                </div>
                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="app-label">Base URL</label>
                        <input type="text" name="base_url" value="{{ form.get('base_url','') }}" class="app-input mt-1" placeholder="https://api.openai.com/v1">
                    </div>
                    <div>
                        <label class="app-label">模型</label>
                        <input type="text" name="model_name" value="{{ form.get('model_name','') }}" class="app-input mt-1" placeholder="gpt-4o-mini">
                    </div>
                </div>
                <p class="text-xs text-gray-500">{{ server_key_hint }}</p>
            </div>
        </details>

        <!-- 5. PIPL 同意 -->
        <div class="border-t border-gray-200 pt-4">
            <label class="flex items-start gap-2 cursor-pointer">
                <input type="checkbox" name="agree" value="on" required class="mt-1" {% if form.get('agree') %}checked{% endif %}>
                <span class="text-xs text-gray-700">
                    我已阅读并同意《<a href="#" class="text-emerald-600 hover:underline">个人信息处理规则</a>》（PIPL）· 洛谷 Cookies 仅用于一次性抓取做题数据 · 报告生成后可随时删除
                </span>
            </label>
        </div>

        <button type="submit" class="app-btn app-btn-primary">🚀 立即生成我的学习报告</button>

        <p class="text-center text-xs text-gray-400">生成后可在 /me/<uid> 查看 3 版本报告（学员·家长·教练）</p>
    </form>

    <div class="text-center">
        <a href="/" class="text-xs text-gray-400 hover:text-emerald-600">← 返回首页</a>
    </div>
</div>
</body>
</html>
"""


@app.route("/select-mode", methods=["GET", "POST"])
def select_mode():
    """v3.5.2 老用户快速入口（已注册选手输 UID 直接看报告，不生成新报告）"""
    import re as _re
    if request.method == "GET":
        return render_template_string(SELECT_MODE_HTML, error=None, form={})
    # POST 接收 luogu_uid → 校验 → 引导身份
    luogu_uid = (request.form.get("luogu_uid") or "").strip()
    if not _re.match(r"^\d{6,10}$", luogu_uid):
        return render_template_string(
            SELECT_MODE_HTML,
            error="请输入 6-10 位洛谷 UID",
            form={"luogu_uid": luogu_uid},
        ), 400
    # 查询是否已注册
    stu = _admin_students.get_student_by_uid(luogu_uid)
    if stu:
        # 已注册 → 进入 /me/<uid>（内部自动引导选身份）
        return redirect(url_for("student_me", luogu_uid=luogu_uid))
    # 未注册 → /register（4 字段·预填 UID）
    return redirect(url_for("register_student") + f"?luogu_uid={luogu_uid}")


SELECT_MODE_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>选择报告版本 · 信竞 AI 报告 v3.5.2</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body{background:linear-gradient(135deg,#ecfdf5 0%,#f0f9ff 100%);min-height:100vh;font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;}
        .card-shadow{box-shadow:0 10px 25px rgba(0,0,0,.06);}
        .big-btn{display:flex;align-items:center;justify-content:center;width:100%;border-radius:12px;padding:14px;font-weight:800;transition:all .15s ease;cursor:pointer;}
        .big-btn-primary{background:linear-gradient(135deg,#059669 0%,#0d9488 100%);color:#fff;}
        .big-btn-primary:hover{transform:translateY(-1px);box-shadow:0 8px 20px rgba(5,150,105,.3);}
        .big-btn-secondary{background:#fff;color:#047857;border:2px solid #6ee7b7;}
        .big-btn-secondary:hover{background:#ecfdf5;}
    </style>
</head>
<body class="p-4">
<div class="max-w-2xl mx-auto py-6 space-y-4">

    <div class="bg-white rounded-2xl card-shadow p-6">
        <div class="text-center mb-4">
            <span class="inline-block px-3 py-1 bg-emerald-100 text-emerald-700 text-xs rounded-full">v3.5.2</span>
            <h1 class="text-2xl font-extrabold text-gray-800 mt-2">🎓 AI 生成学习报告</h1>
            <p class="text-sm text-gray-500 mt-1">基于洛谷做题数据 + 30 秒 AI 分析</p>
        </div>

        <form method="post" class="space-y-3">
            <div>
                <label class="block text-sm font-bold text-gray-700 mb-1">洛谷 UID <span class="text-red-500">*</span></label>
                <input name="luogu_uid" inputmode="numeric" pattern="\\d{6,10}" required placeholder="请输入 6-10 位数字 UID" class="w-full border border-gray-300 rounded-lg px-3 py-2.5 focus:border-emerald-500 focus:ring-2 focus:ring-emerald-200" value="{{ form.get('luogu_uid','') }}">
                <p class="text-xs text-gray-400 mt-1">首次使用？先输入 UID，系统会引导你完成 4 字段注册（地域/年龄是报告核心数据）</p>
            </div>

            {% if error %}
            <div class="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg p-2">{{ error }}</div>
            {% endif %}

            <button type="submit" class="big-btn big-btn-primary">🚀 立即生成我的学习报告</button>
        </form>
    </div>

    <div class="bg-white rounded-2xl card-shadow p-5 text-center">
        <p class="text-xs text-gray-500">没有洛谷账号？</p>
        <a href="/register" class="text-emerald-600 hover:underline text-sm">前往 4 字段极简注册 →</a>
    </div>

    <p class="text-center text-xs text-gray-400">信竞 AI 报告 · v3.5.2</p>
</div>
</body>
</html>
"""


@app.route("/redeem", methods=["GET", "POST"])
def redeem_code():
    """v3.5.2 全局兑换码激活入口

    支持 SKU（数据库实际值）：
      · parent_sub         → 家长订阅（完整报告 + 解锁选手 AI 讲题）
      · popularize_camp    → 普及组冲刺营（4 周 → CSP-J 免初赛）
      · improve_camp       → 提高组冲刺营（8 周 → CSP-S 免初赛）

    流程：
      1. 用户输入兑换码 + 自己的洛谷 UID
      2. 系统校验码有效 + 未被使用
      3. 激活相应权限（写入 redeemed_at + student_id）
    """
    prefill_code = (request.args.get("code") or "").strip()
    error = None
    success = None
    student_uid = ""

    if request.method == "POST":
        code = (request.form.get("code") or "").strip().upper()
        student_uid = (request.form.get("student_uid") or "").strip()

        if not code or not code.replace("-", "").replace("_", "").isalnum():
            error = "兑换码格式错误（应为 字母-XXXX-XXXX 形式）"
        elif not student_uid or not student_uid.isdigit() or not (6 <= len(student_uid) <= 10):
            error = "请填写 6-10 位洛谷 UID"
        else:
            try:
                from task_store import _get_conn
                # 1. 查 code
                conn = _get_conn()
                try:
                    row = conn.execute(
                        "SELECT * FROM activation_codes WHERE code = ?",
                        (code,),
                    ).fetchone()
                finally:
                    conn.close()
                if not row:
                    error = f"兑换码 {code} 不存在或已失效"
                else:
                    row_dict = dict(row)
                    if row_dict.get("redeemed_at"):
                        error = f"兑换码 {code} 已被使用（{row_dict.get('redeemed_at')}）"
                    else:
                        # 2. 查 student.id
                        from admin_students import get_student_by_uid
                        stu = get_student_by_uid(student_uid)
                        if not stu:
                            error = f"洛谷 UID {student_uid} 未注册，请先在首页「我是选手」注册"
                        else:
                            # 3. 激活：更新 redeemed_at + student_id
                            conn = _get_conn()
                            try:
                                conn.execute(
                                    "UPDATE activation_codes "
                                    "SET redeemed_at = datetime('now'), "
                                    "    student_id = ?, "
                                    "    expires_at = datetime('now', '+' || duration_days || ' days') "
                                    "WHERE code = ?",
                                    (stu["id"], code),
                                )
                                conn.commit()
                            finally:
                                conn.close()
                            success = {
                                "code": code,
                                "sku": row_dict.get("sku", "parent_sub"),
                                "student_uid": student_uid,
                            }
            except Exception as e:
                error = f"兑换失败：{e}"

    return render_template_string(
        REDEEM_HTML,
        prefill_code=prefill_code,
        error=error,
        success=success,
        student_uid=student_uid,
    )


@app.route("/coach")
def coach_landing():
    """v3.5.2 教练版咨询入口（B2B · 联系客服购买）"""
    return render_template_string(COACH_LANDING_HTML)


REDEEM_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>兑换码激活 · 信竞 AI 报告</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body{font-family:-appleSystem,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;background:linear-gradient(135deg,#fef3c7 0%,#fed7aa 100%);min-height:100vh;}
        .sku-card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:14px;text-align:center;}
        .sku-card.pro{border-color:#f59e0b;background:linear-gradient(135deg,#fffbeb,#fef3c7);}
        .sku-card.parent{border-color:#3b82f6;background:linear-gradient(135deg,#eff6ff,#dbeafe);}
        .sku-card.camp-j{border-color:#a855f7;background:linear-gradient(135deg,#faf5ff,#f3e8ff);}
        .sku-card.camp-s{border-color:#ef4444;background:linear-gradient(135deg,#fef2f2,#fee2e2);}
    </style>
</head>
<body class="flex items-center justify-center p-4">
    <div class="bg-white rounded-2xl shadow-lg p-8 w-full max-w-2xl">
        <div class="text-center mb-5">
            <div class="inline-block px-3 py-1 bg-amber-100 text-amber-700 text-xs rounded-full mb-2">v3.5.2 · 兑换码激活</div>
            <h1 class="text-2xl font-bold text-gray-800 mb-1">🎁 兑换码激活</h1>
            <p class="text-sm text-gray-500">输入您的兑换码 + 洛谷 UID，立即解锁对应功能</p>
        </div>

        {% if error %}
        <div class="mb-4 px-3 py-2 bg-red-50 border border-red-200 text-red-700 rounded-lg text-sm">⚠️ {{ error }}</div>
        {% endif %}

        {% if success %}
        <div class="mb-5 px-4 py-3 bg-emerald-50 border border-emerald-200 text-emerald-700 rounded-lg">
            <div class="font-bold text-base mb-1">✅ 激活成功！</div>
            <p class="text-sm">兑换码 <code class="font-mono">{{ success.code }}</code> 已绑定到 UID <code class="font-mono">{{ success.student_uid }}</code></p>
            <p class="text-sm mt-1">SKU：<strong>{{ success.sku }}</strong></p>
            <div class="mt-3 flex gap-2">
                <a href="/me/{{ success.student_uid }}" class="px-3 py-1.5 bg-emerald-600 text-white text-sm rounded-md hover:bg-emerald-700">→ 进入个人中心</a>
                <a href="/" class="px-3 py-1.5 border border-gray-300 text-gray-700 text-sm rounded-md hover:bg-gray-50">返回首页</a>
            </div>
        </div>
        {% endif %}

        {% if not success %}
        <form method="POST" class="space-y-3 mb-5">
            <div>
                    <label class="block text-sm font-medium text-gray-700 mb-1">兑换码</label>
                    <input type="text" name="code" required minlength="8" maxlength="64"
                           value="{{ prefill_code or '' }}"
                           placeholder="如：PARENT-SUB-XXXX 或 PS-XXXXXXXX"
                           class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono focus:ring-2 focus:ring-amber-500 focus:border-amber-500">
                </div>
            <div>
                <label class="block text-sm font-medium text-gray-700 mb-1">洛谷 UID（6-10 位）</label>
                <input type="text" name="student_uid" required pattern="\\d{6,10}" inputmode="numeric"
                       value="{{ student_uid or '' }}"
                       placeholder="绑定到哪位选手的账号"
                       class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono focus:ring-2 focus:ring-amber-500 focus:border-amber-500">
                <p class="text-xs text-gray-400 mt-1">激活后功能将绑定到该 UID</p>
            </div>
            <button type="submit" class="w-full bg-gradient-to-r from-amber-500 to-amber-600 text-white font-bold py-2.5 rounded-lg hover:from-amber-600 hover:to-amber-700 transition">
                🎁 激活兑换码
            </button>
        </form>
        {% endif %}

        <div class="border-t border-gray-200 pt-4">
            <p class="text-xs text-gray-500 mb-2">支持以下 SKU 类型：</p>
            <div class="grid grid-cols-1 md:grid-cols-3 gap-2 text-xs">
                <div class="sku-card parent">
                    <div class="font-bold text-blue-700">家长订阅</div>
                    <div class="text-gray-500 mt-1">PS-XXXXXXXX</div>
                    <div class="text-blue-600 text-xs mt-2">
                        完整报告 + 周报 + 倒推<br>
                        <strong>+ 解锁选手 AI 讲题</strong>
                    </div>
                </div>
                <div class="sku-card camp-j">
                    <div class="font-bold text-purple-700">普及冲刺</div>
                    <div class="text-gray-500 mt-1">PJC-XXXXXXXX</div>
                    <div class="text-purple-600 text-xs mt-2">
                        4 周 · GESP 7 级 80+<br>
                        → 9 月 CSP-J 免初赛
                    </div>
                </div>
                <div class="sku-card camp-s">
                    <div class="font-bold text-red-700">提高冲刺</div>
                    <div class="text-gray-500 mt-1">IC-XXXXXXXX</div>
                    <div class="text-red-600 text-xs mt-2">
                        8 周 · GESP 8 级 80+<br>
                        → 9 月 CSP-S 免初赛
                    </div>
                </div>
            </div>
            <p class="text-xs text-gray-400 mt-3 text-center">
                💡 AI 讲题已含在「家长订阅」内 · <a href="/" class="text-amber-600 hover:underline">加 V 获取</a>（家长）· <a href="/coach" class="text-emerald-600 hover:underline">联系客服</a>（教练）
            </p>
        </div>
    </div>
</body>
</html>
"""


COACH_LANDING_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>教练版咨询 · 信竞 AI 报告 · v3.5.2</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body{font-family:-appleSystem,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;background:linear-gradient(135deg,#e0e7ff 0%,#cffafe 100%);min-height:100vh;}
    </style>
</head>
<body class="flex items-center justify-center p-4">
    <div class="bg-white rounded-2xl shadow-lg p-8 w-full max-w-2xl">
        <div class="text-center mb-6">
            <div class="inline-block px-3 py-1 bg-indigo-100 text-indigo-700 text-xs rounded-full mb-2">v3.5.2 · 教练版 B2B</div>
            <h1 class="text-3xl font-bold text-gray-800 mb-2">🎯 教练版咨询</h1>
            <p class="text-sm text-gray-600">批量学员管理 · 兑换码生成 · 营收看板 · 1v1 客户经理</p>
        </div>

        <div class="grid grid-cols-2 md:grid-cols-3 gap-3 mb-6 text-center text-xs">
            <div class="bg-emerald-50 border border-emerald-200 rounded-lg p-3">
                <div class="text-2xl mb-1">👥</div>
                <div class="font-bold text-emerald-700">批量学员管理</div>
                <div class="text-gray-500 mt-1">无上限 · 一键录入</div>
            </div>
            <div class="bg-amber-50 border border-amber-200 rounded-lg p-3">
                <div class="text-2xl mb-1">🎁</div>
                <div class="font-bold text-amber-700">兑换码生成</div>
                <div class="text-gray-500 mt-1">学员 Pro / 家长订阅</div>
            </div>
            <div class="bg-blue-50 border border-blue-200 rounded-lg p-3">
                <div class="text-2xl mb-1">📊</div>
                <div class="font-bold text-blue-700">营收看板</div>
                <div class="text-gray-500 mt-1">日 / 周 / 月数据</div>
            </div>
            <div class="bg-purple-50 border border-purple-200 rounded-lg p-3">
                <div class="text-2xl mb-1">📅</div>
                <div class="font-bold text-purple-700">倒推计划</div>
                <div class="text-gray-500 mt-1">CSP/NOIP 自动规划</div>
            </div>
            <div class="bg-pink-50 border border-pink-200 rounded-lg p-3">
                <div class="text-2xl mb-1">📨</div>
                <div class="font-bold text-pink-700">周报推送</div>
                <div class="text-gray-500 mt-1">家长周报自动生成</div>
            </div>
            <div class="bg-indigo-50 border border-indigo-200 rounded-lg p-3">
                <div class="text-2xl mb-1">🤝</div>
                <div class="font-bold text-indigo-700">1v1 客户经理</div>
                <div class="text-gray-500 mt-1">专属服务群</div>
            </div>
        </div>

        <div class="bg-gradient-to-r from-indigo-50 to-cyan-50 border border-indigo-200 rounded-lg p-4 mb-5">
            <div class="font-bold text-indigo-800 mb-2">💼 计费模式（B2B · 谈单制）</div>
            <div class="text-sm text-gray-700 space-y-1">
                <div>· <strong>基础版</strong>：管理 ≤20 学员 / 月 · 适合个人教练</div>
                <div>· <strong>机构版</strong>：管理 ≤100 学员 / 月 · 适合中小机构</div>
                <div>· <strong>旗舰版</strong>：管理 ≤500 学员 / 月 · 适合大型机构</div>
            </div>
            <p class="text-xs text-gray-500 mt-2">💡 具体价格联系客服 · 1v1 谈单 · 不挂网价</p>
        </div>

        <div class="space-y-3 mb-5">
            <div class="bg-white border border-gray-300 rounded-lg p-4 flex items-center gap-3">
                <div class="text-2xl">📞</div>
                <div class="flex-1">
                    <div class="text-xs text-gray-500">客户经理电话</div>
                    <div class="font-mono font-bold text-gray-800">400-XXX-XXXX 转 1</div>
                </div>
            </div>
            <div class="bg-white border border-gray-300 rounded-lg p-4 flex items-center gap-3">
                <div class="text-2xl">📧</div>
                <div class="flex-1">
                    <div class="text-xs text-gray-500">商务邮箱</div>
                    <div class="font-mono font-bold text-gray-800">coach@xinjing-ai.com</div>
                </div>
            </div>
            <div class="bg-white border border-gray-300 rounded-lg p-4 flex items-center gap-3">
                <div class="text-2xl">🆚</div>
                <div class="flex-1">
                    <div class="text-xs text-gray-500">商务微信</div>
                    <div class="font-mono font-bold text-gray-800">xinjing-ai-business</div>
                </div>
            </div>
        </div>

        <div class="text-center text-xs text-gray-500 pt-4 border-t border-gray-200 space-x-3">
            <span>已有教练账号？<a href="/admin/login" class="text-indigo-600 hover:underline">/admin/login</a></span>
            <span class="text-gray-300">·</span>
            <a href="/" class="text-gray-500 hover:text-emerald-600 hover:underline">返回首页</a>
        </div>
    </div>
</body>
</html>
"""


# ============================================================
# v3.5 Phase 2 · 家长端 + 学员目标 + 周报 + 跳级决策树
# ============================================================
# 路由：
#  - /admin/students/<id>/guardians                家长列表 + 新建
#  - /admin/students/<id>/guardians/<gid>/delete   删除家长（POST）
#  - /admin/students/<id>/guardians/<gid>/rotate   重置 token（POST）
#  - /admin/students/<id>/goal                     学员目标路径（GET/POST）
#  - /admin/students/<id>/reports                  周报列表
#  - /admin/students/<id>/reports/generate         立即生成周报（POST）
#  - /parent/<token>                                家长无登录面板首页
#  - /parent/<token>/report/<rid>                  查看单份周报（HTML）+ 打开数 +1
# ============================================================


@app.route("/admin/students/<int:student_id>/guardians", methods=["GET", "POST"])
def admin_students_guardians(student_id: int):
    auth_redirect = require_admin_auth()
    if auth_redirect is not None:
        return auth_redirect
    student = _admin_students.get_student(student_id)
    if not student:
        return redirect(
            url_for("admin_students_list", notice=f"学员 {student_id} 不存在", notice_type="error")
        )
    if request.method == "POST":
        try:
            ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
            g = _admin_guardians.create_guardian(
                student_id=student_id,
                phone=str(request.form.get("phone", "") or "").strip() or None,
                email=str(request.form.get("email", "") or "").strip() or None,
                display_name=str(request.form.get("display_name", "") or "").strip() or None,
                notify_channel=str(request.form.get("notify_channel", "email") or "email").strip(),
                consent_ip=ip,
            )
            return redirect(
                url_for(
                    "admin_students_guardians",
                    student_id=student_id,
                    notice=f"已添加家长（id={g['id']}），token 有效期至 {g['notify_token_expires_at']}",
                    notice_type="success",
                )
            )
        except ValueError as exc:
            guardians = _admin_guardians.list_guardians_by_student(student_id)
            return render_template_string(
                ADMIN_STUDENTS_GUARDIANS_HTML,
                student=student,
                guardians=guardians,
                error=str(exc),
                notice="",
                notice_type="error",
            )
    guardians = _admin_guardians.list_guardians_by_student(student_id)
    return render_template_string(
        ADMIN_STUDENTS_GUARDIANS_HTML,
        student=student,
        guardians=guardians,
        error="",
        notice=str(request.args.get("notice", "") or ""),
        notice_type=str(request.args.get("notice_type", "") or "success"),
    )


@app.route("/admin/students/<int:student_id>/guardians/<int:guardian_id>/delete", methods=["POST"])
def admin_students_guardians_delete(student_id: int, guardian_id: int):
    auth_redirect = require_admin_auth()
    if auth_redirect is not None:
        return auth_redirect
    _admin_guardians.delete_guardian(guardian_id)
    return redirect(
        url_for("admin_students_guardians", student_id=student_id, notice="家长已删除", notice_type="success")
    )


@app.route("/admin/students/<int:student_id>/guardians/<int:guardian_id>/rotate", methods=["POST"])
def admin_students_guardians_rotate(student_id: int, guardian_id: int):
    auth_redirect = require_admin_auth()
    if auth_redirect is not None:
        return auth_redirect
    new_token = _admin_guardians.rotate_token(guardian_id)
    return redirect(
        url_for(
            "admin_students_guardians",
            student_id=student_id,
            notice=f"新 token 已生成（前 12 位 {new_token[:12]}...），旧 token 立即失效",
            notice_type="success",
        )
    )


@app.route("/admin/students/<int:student_id>/goal", methods=["GET", "POST"])
def admin_students_goal(student_id: int):
    auth_redirect = require_admin_auth()
    if auth_redirect is not None:
        return auth_redirect
    student = _admin_students.get_student(student_id)
    if not student:
        return redirect(
            url_for("admin_students_list", notice=f"学员 {student_id} 不存在", notice_type="error")
        )
    if request.method == "POST":
        _admin_goals.upsert_student_goal(
            student_id=student_id,
            primary_path=str(request.form.get("primary_path", "未决定") or "未决定"),
            target_university=str(request.form.get("target_university", "") or "").strip() or None,
            target_province=str(request.form.get("target_province", "") or "").strip() or None,
            notes=str(request.form.get("notes", "") or "").strip() or None,
        )
        return redirect(
            url_for("admin_students_goal", student_id=student_id, notice="学员目标已保存", notice_type="success")
        )
    goal = _admin_goals.get_student_goal(student_id) or {}
    rec = _admin_goals.recommend_skip_path(student_id)
    return render_template_string(
        ADMIN_STUDENTS_GOAL_HTML,
        student=student,
        goal=goal,
        rec=rec,
        primary_paths=sorted(_admin_goals.ALLOWED_PRIMARY_PATHS),
        sample_universities=_admin_goals.SAMPLE_UNIVERSITIES,
        notice=str(request.args.get("notice", "") or ""),
        notice_type=str(request.args.get("notice_type", "") or "success"),
    )


@app.route("/admin/students/<int:student_id>/reports", methods=["GET"])
def admin_students_reports(student_id: int):
    auth_redirect = require_admin_auth()
    if auth_redirect is not None:
        return auth_redirect
    student = _admin_students.get_student(student_id)
    if not student:
        return redirect(
            url_for("admin_students_list", notice=f"学员 {student_id} 不存在", notice_type="error")
        )
    reports = _weekly_reports.list_weekly_reports(student_id, limit=20)
    return render_template_string(
        ADMIN_STUDENTS_REPORTS_HTML,
        student=student,
        reports=reports,
        notice=str(request.args.get("notice", "") or ""),
        notice_type=str(request.args.get("notice_type", "") or "success"),
    )


@app.route("/admin/students/<int:student_id>/reports/generate", methods=["POST"])
def admin_students_reports_generate(student_id: int):
    auth_redirect = require_admin_auth()
    if auth_redirect is not None:
        return auth_redirect
    data = _weekly_reports.build_report_data(student_id)
    if "error" in data:
        return redirect(
            url_for("admin_students_reports", student_id=student_id, notice=data["error"], notice_type="error")
        )
    html = _weekly_reports.render_report_html(data)
    ws = _weekly_reports.get_week_start()
    result = _weekly_reports.save_report(student_id, ws, html)
    return redirect(
        url_for(
            "admin_students_reports",
            student_id=student_id,
            notice=f"已生成 {ws.isoformat()} 周报（id={result['id']}）",
            notice_type="success",
        )
    )


# ============================================================
# v3.5.2 学员 4 字段极简注册（学而思图 1 模式） + /me 自助入口
# ============================================================
# 路由：
#  - GET  /register                       极简注册表单（城市/姓名/年级/性别 + 洛谷 UID + 微信/手机）
#  - POST /register                       提交注册 → 重定向 /me/<luogu_uid>
#  - GET  /me/<luogu_uid>                 学员 Pro 自助面板（段位 + 错题本 + 订阅 CTA）
# ============================================================


# v3.5.2: 中国主要城市白名单（学而思图 1 风格 · 防止乱填）
# 结构：[(省份/直辖市/特别行政区 label, [城市, ...]), ...]
# 覆盖 4 直辖市 + 23 省 + 5 自治区 + 2 特别行政区 = 34 个省级行政区 · ~290 个地级市
CITIES_REGISTRATION = [
    # ---- 4 直辖市 ----
    ("直辖市", ["北京", "上海", "天津", "重庆"]),
    # ---- 2 特别行政区 ----
    ("港澳台", ["香港", "澳门", "台北", "高雄", "台中", "台南"]),
    # ---- 5 自治区 ----
    ("新疆", ["乌鲁木齐", "克拉玛依", "吐鲁番", "哈密", "阿克苏", "喀什", "和田", "伊宁", "塔城", "阿勒泰", "石河子", "阿拉尔", "图木舒克", "五家渠", "北屯", "铁门关", "双河", "可克达拉"]),
    ("西藏", ["拉萨", "日喀则", "昌都", "林芝", "山南", "那曲", "阿里", "江孜"]),
    ("内蒙古", ["呼和浩特", "包头", "乌海", "赤峰", "通辽", "鄂尔多斯", "呼伦贝尔", "巴彦淖尔", "乌兰察布", "兴安盟", "锡林郭勒", "阿拉善"]),
    ("广西", ["南宁", "柳州", "桂林", "梧州", "北海", "防城港", "钦州", "贵港", "玉林", "百色", "贺州", "河池", "来宾", "崇左"]),
    ("宁夏", ["银川", "石嘴山", "吴忠", "固原", "中卫"]),
    # ---- 23 省（按 2025 行政区划代码）----
    ("河北", ["石家庄", "唐山", "秦皇岛", "邯郸", "邢台", "保定", "张家口", "承德", "沧州", "廊坊", "衡水", "辛集", "藁城", "晋州", "新乐", "鹿泉", "遵化", "迁安", "武安", "南宫", "沙河", "涿州", "定州", "安国", "高碑店", "泊头", "任丘", "黄骅", "河间", "霸州", "三河", "冀州", "深州"]),
    ("山西", ["太原", "大同", "阳泉", "长治", "晋城", "朔州", "晋中", "运城", "忻州", "临汾", "吕梁", "古交", "介休", "永济", "河津", "原平", "侯马", "霍州", "孝义", "汾阳"]),
    ("辽宁", ["沈阳", "大连", "鞍山", "抚顺", "本溪", "丹东", "锦州", "营口", "阜新", "辽阳", "盘锦", "铁岭", "朝阳", "葫芦岛", "瓦房店", "普兰店", "庄河", "海城", "东港", "凤城", "凌海", "北镇", "大石桥", "盖州", "灯塔", "调兵山", "开原", "北票", "凌源"]),
    ("吉林", ["长春", "吉林", "四平", "辽源", "通化", "白山", "松原", "白城", "延边", "延吉", "图们", "敦化", "珲春", "龙井", "和龙", "公主岭", "梅河口", "集安", "桦甸", "舒兰", "磐石", "洮南", "大安", "临江"]),
    ("黑龙江", ["哈尔滨", "齐齐哈尔", "鸡西", "鹤岗", "双鸭山", "大庆", "伊春", "佳木斯", "七台河", "牡丹江", "黑河", "绥化", "大兴安岭", "绥芬河", "海林", "宁安", "穆棱", "东宁", "五大连池", "北安", "铁力", "同江", "富锦", "虎林", "密山", "萝北", "绥滨", "肇东", "安达", "肇源", "海伦", "望奎"]),
    ("江苏", ["南京", "无锡", "徐州", "常州", "苏州", "南通", "连云港", "淮安", "盐城", "扬州", "镇江", "泰州", "宿迁", "江阴", "宜兴", "邳州", "新沂", "金坛", "溧阳", "常熟", "张家港", "昆山", "太仓", "启东", "如皋", "海门", "东台", "仪征", "高邮", "扬中", "句容", "兴化", "靖江", "泰兴", "沭阳", "泗阳", "泗洪"]),
    ("浙江", ["杭州", "宁波", "温州", "嘉兴", "湖州", "绍兴", "金华", "衢州", "舟山", "台州", "丽水", "建德", "余姚", "慈溪", "瑞安", "乐清", "海宁", "平湖", "桐乡", "诸暨", "嵊州", "兰溪", "义乌", "东阳", "永康", "江山", "温岭", "临海", "玉环", "龙泉"]),
    ("安徽", ["合肥", "芜湖", "蚌埠", "淮南", "马鞍山", "淮北", "铜陵", "安庆", "黄山", "滁州", "阜阳", "宿州", "六安", "亳州", "池州", "宣城", "界首", "天长", "明光", "桐城", "宁国", "广德"]),
    ("福建", ["福州", "厦门", "莆田", "三明", "泉州", "漳州", "南平", "龙岩", "宁德", "福清", "长乐", "永安", "石狮", "晋江", "南安", "龙海", "邵武", "武夷山", "建瓯", "漳平", "福鼎", "福安"]),
    ("江西", ["南昌", "景德镇", "萍乡", "九江", "新余", "鹰潭", "赣州", "吉安", "宜春", "抚州", "上饶", "瑞昌", "乐平", "瑞金", "井冈山", "高安", "樟树", "丰城", "德兴", "庐山"]),
    ("山东", ["济南", "青岛", "淄博", "枣庄", "东营", "烟台", "潍坊", "济宁", "泰安", "威海", "日照", "临沂", "德州", "聊城", "滨州", "菏泽", "章丘", "胶州", "即墨", "平度", "莱西", "滕州", "龙口", "莱阳", "莱州", "蓬莱", "招远", "栖霞", "海阳", "青州", "诸城", "寿光", "安丘", "高密", "昌邑", "曲阜", "邹城", "新泰", "肥城", "乳山", "文登", "荣成", "乐陵", "禹城", "临清", "高唐", "邹平"]),
    ("河南", ["郑州", "开封", "洛阳", "平顶山", "安阳", "鹤壁", "新乡", "焦作", "濮阳", "许昌", "漯河", "三门峡", "南阳", "商丘", "信阳", "周口", "驻马店", "济源", "巩义", "兰考", "汝州", "邓州", "永城", "禹州", "长葛", "许昌县", "尉氏", "新郑", "登封", "新密", "荥阳", "中牟", "偃师", "孟州", "沁阳", "卫辉", "辉县", "林州", "滑县", "汤阴", "内黄", "清丰", "南乐", "范县", "台前", "濮阳县", "长垣", "封丘", "原阳", "延津", "获嘉", "修武", "武陟", "温县", "博爱", "沁阳", "孟州"]),
    ("湖北", ["武汉", "黄石", "十堰", "宜昌", "襄阳", "鄂州", "荆门", "孝感", "荆州", "黄冈", "咸宁", "随州", "恩施", "仙桃", "潜江", "天门", "神农架", "大冶", "丹江口", "宜城", "老河口", "枣阳", "宜都", "枝江", "当阳", "荆州", "洪湖", "松滋", "钟祥", "京山", "应城", "云梦", "汉川", "石首", "监利", "公安", "江陵", "麻城", "武穴", "红安", "罗田", "浠水", "蕲春", "黄梅", "英山", "团风", "崇阳", "通城", "通山", "赤壁", "嘉鱼", "广水", "随县", "恩施", "利川"]),
    ("湖南", ["长沙", "株洲", "湘潭", "衡阳", "邵阳", "岳阳", "常德", "张家界", "益阳", "郴州", "永州", "怀化", "娄底", "湘西", "浏阳", "醴陵", "韶山", "湘乡", "耒阳", "常宁", "武冈", "邵东", "临湘", "汨罗", "岳阳", "津市", "澧县", "安乡", "汉寿", "桃源", "石门", "慈利", "桑植", "沅江", "资兴", "永兴", "宜章", "桂阳", "嘉禾", "临武", "汝城", "桂东", "安仁", "资兴", "冷水滩", "祁阳", "东安", "双牌", "道县", "江永", "宁远", "蓝山", "新田", "江华", "怀化", "洪江", "沅陵", "溆浦", "会同", "新晃", "芷江", "靖州", "通道", "娄底", "冷水江", "涟源", "双峰", "新化", "吉首", "泸溪", "凤凰", "花垣", "保靖", "古丈", "永顺", "龙山"]),
    ("广东", ["广州", "深圳", "珠海", "汕头", "韶关", "佛山", "江门", "湛江", "茂名", "肇庆", "惠州", "梅州", "汕尾", "河源", "阳江", "清远", "东莞", "中山", "潮州", "揭阳", "云浮", "从化", "增城", "英德", "连州", "乐昌", "南雄", "高要", "四会", "罗定", "普宁", "陆丰", "阳春", "恩平", "台山", "开平", "鹤山", "高明", "三水", "顺德", "南海", "番禺", "花都", "白云", "黄埔", "天河", "海珠", "越秀", "荔湾", "福田", "罗湖", "南山", "盐田", "宝安", "龙岗", "龙华", "坪山", "光明", "大鹏新区"]),
    ("海南", ["海口", "三亚", "三沙", "儋州", "五指山", "琼海", "文昌", "万宁", "东方", "定安", "屯昌", "澄迈", "临高", "白沙", "昌江", "乐东", "陵水", "保亭", "琼中"]),
    ("四川", ["成都", "自贡", "攀枝花", "泸州", "德阳", "绵阳", "广元", "遂宁", "内江", "乐山", "南充", "眉山", "宜宾", "广安", "达州", "雅安", "巴中", "资阳", "阿坝", "甘孜", "凉山", "都江堰", "彭州", "邛崃", "崇州", "广汉", "什邡", "绵竹", "江油", "阆中", "华蓥", "峨眉山", "万源", "简阳", "西昌", "康定", "马尔康"]),
    ("贵州", ["贵阳", "六盘水", "遵义", "安顺", "铜仁", "毕节", "黔西南", "黔东南", "黔南", "兴义", "凯里", "都匀", "福泉", "清镇", "赤水", "仁怀", "兴仁", "盘州", "兴义", "安龙", "册亨", "望谟", "贞丰", "晴隆", "普安", "关岭", "紫云", "镇宁", "平坝", "普定", "西秀", "平坝", "关岭", "紫云", "镇宁"]),
    ("云南", ["昆明", "曲靖", "玉溪", "保山", "昭通", "丽江", "普洱", "临沧", "楚雄", "红河", "文山", "西双版纳", "大理", "德宏", "怒江", "迪庆", "安宁", "腾冲", "宣威", "水富", "瑞丽", "芒市", "泸水", "香格里拉", "大理", "个旧", "开远", "蒙自", "弥勒", "文山", "景洪", "普洱", "思茅", "临沧", "景东", "江城", "孟连", "澜沧", "西盟", "勐海", "勐腊", "勐海"]),
    ("陕西", ["西安", "铜川", "宝鸡", "咸阳", "渭南", "延安", "汉中", "榆林", "安康", "商洛", "韩城", "华阴", "兴平", "彬州", "神木", "府谷", "靖边", "定边", "绥德", "米脂", "佳县", "吴堡", "清涧", "子洲", "横山", "榆阳", "汉台", "南郑", "城固", "洋县", "西乡", "勉县", "宁强", "略阳", "镇巴", "留坝", "佛坪", "安康", "汉阴", "石泉", "宁陕", "紫阳", "岚皋", "平利", "镇坪", "旬阳", "白河", "商州", "洛南", "丹凤", "商南", "山阳", "镇安", "柞水", "西安", "高陵", "蓝田", "鄠邑", "周至", "阎良", "临潼", "长安", "碑林", "莲湖", "灞桥", "未央", "雁塔", "新城区", "阎良区"]),
    ("甘肃", ["兰州", "嘉峪关", "金昌", "白银", "天水", "武威", "张掖", "平凉", "酒泉", "庆阳", "定西", "陇南", "临夏", "甘南", "玉门", "敦煌", "华亭", "合作", "西峰", "崆峒", "秦州", "麦积", "甘州", "肃州", "凉州", "武都", "成县", "文县", "宕昌", "康县", "西和", "礼县", "徽县", "两当", "华池", "合水", "正宁", "宁县", "镇原", "环县", "庆城", "临夏市", "临夏县", "康乐", "永靖", "广河", "和政", "东乡族自治县", "积石山", "合作", "临潭", "卓尼", "舟曲", "迭部", "玛曲", "碌曲", "夏河", "兰州新区"]),
    ("青海", ["西宁", "海东", "海北", "海南", "黄南", "果洛", "玉树", "海西", "格尔木", "德令哈", "茫崖", "大柴旦", "冷湖"]),
    ("其他", ["外籍", "其他"]),
]

# v3.5.2: 完整学制（小学一年级 → 大四）
# 用户要求"从一年级到大四"，覆盖 K12 + 大学，方便不同学段学员
GRADES_REGISTRATION = [
    # 小学
    ("PRIMARY_1", "小学一年级"),
    ("PRIMARY_2", "小学二年级"),
    ("PRIMARY_3", "小学三年级"),
    ("PRIMARY_4", "小学四年级"),
    ("PRIMARY_5", "小学五年级"),
    ("PRIMARY_6", "小学六年级"),
    # 初中
    ("JUNIOR_1", "初一（初中一年级）"),
    ("JUNIOR_2", "初二（初中二年级）"),
    ("JUNIOR_3", "初三（初中三年级）"),
    # 高中
    ("SENIOR_1", "高一（高中一年级）"),
    ("SENIOR_2", "高二（高中二年级）"),
    ("SENIOR_3", "高三（高中三年级）"),
    # 大学
    ("UNIV_1", "大一（大学一年级）"),
    ("UNIV_2", "大二（大学二年级）"),
    ("UNIV_3", "大三（大学三年级）"),
    ("UNIV_4", "大四（大学四年级）"),
    # 其他
    ("GRADUATED", "已毕业"),
]


def _validate_birth_date(bd_str: str) -> tuple[bool, str, bool]:
    """返回 (ok, normalized_or_reason, is_minor)"""
    if not bd_str:
        return (True, "", False)
    try:
        bd = datetime.strptime(bd_str, "%Y-%m-%d").date()
    except ValueError:
        return (False, "出生日期格式错误（应为 YYYY-MM-DD）", False)
    today = date.today()
    age = (today - bd).days / 365.25
    is_minor = age < 14
    return (True, bd.isoformat(), is_minor)


def _flatten_cities() -> set[str]:
    """把 [(group_label, [cities]), ...] 拍平为 set 用于 O(1) 校验"""
    s: set[str] = set()
    for _group, cities in CITIES_REGISTRATION:
        s.update(cities)
    return s


_CITIES_FLAT = _flatten_cities()


def _city_to_province(city: str | None) -> str | None:
    """反向查：城市 → 省份（用于 /me 显示）"""
    if not city:
        return None
    for group, cities in CITIES_REGISTRATION:
        if city in cities:
            return group
    return None


def _grade_to_label(grade: str | None) -> str | None:
    """grade value → 中文 label"""
    if not grade:
        return None
    for v, label in GRADES_REGISTRATION:
        if v == grade:
            return label
    return grade   # 未知值原样返回


@app.route("/register", methods=["GET", "POST"])
def register_student():
    """v3.5.2 学员 4 字段极简注册（学而思图 1 模式）

    流程：
      1. 必填 4 项：城市 / 姓名 / 年级 / 性别
      2. 可选 3 项：洛谷 UID（必填，借力主站实名）/ 微信扫码 / 手机号
      3. 提交后：去重（luogu_uid 已存在则提示）→ 写入 students
      4. 重定向：/me/<luogu_uid>
    """
    if request.method == "GET":
        return render_template_string(
            REGISTER_HTML,
            cities=CITIES_REGISTRATION,
            grades=GRADES_REGISTRATION,
            error=None,
            form={},
        )

    # ---- POST 处理 ----
    form = {
        "city": (request.form.get("city") or "").strip(),
        "real_name": (request.form.get("real_name") or "").strip(),
        "grade": (request.form.get("grade") or "").strip(),
        "gender": (request.form.get("gender") or "").strip(),
        "luogu_uid": (request.form.get("luogu_uid") or "").strip(),
        "wechat_openid": (request.form.get("wechat_openid") or "").strip(),
        "phone": (request.form.get("phone") or "").strip(),
        "birth_date": (request.form.get("birth_date") or "").strip(),
        "agree": request.form.get("agree") == "on",
    }

    # ---- 校验 ----
    if not form["city"] or form["city"] not in _CITIES_FLAT:
        return render_template_string(REGISTER_HTML, cities=CITIES_REGISTRATION, grades=GRADES_REGISTRATION, error="请选择城市", form=form)
    if not form["real_name"]:
        return render_template_string(REGISTER_HTML, cities=CITIES_REGISTRATION, grades=GRADES_REGISTRATION, error="姓名必填", form=form)
    if not form["grade"]:
        return render_template_string(REGISTER_HTML, cities=CITIES_REGISTRATION, grades=GRADES_REGISTRATION, error="年级必填", form=form)
    if form["gender"] not in ("M", "F"):
        return render_template_string(REGISTER_HTML, cities=CITIES_REGISTRATION, grades=GRADES_REGISTRATION, error="请选择性别", form=form)
    if not form["luogu_uid"] or not form["luogu_uid"].isdigit() or not (6 <= len(form["luogu_uid"]) <= 10):
        return render_template_string(REGISTER_HTML, cities=CITIES_REGISTRATION, grades=GRADES_REGISTRATION, error="洛谷 UID 必填（6-10 位数字）", form=form)
    if not form["agree"]:
        return render_template_string(REGISTER_HTML, cities=CITIES_REGISTRATION, grades=GRADES_REGISTRATION, error="请勾选《用户协议》和《PIPL 知情同意书》", form=form)

    # 出生日期（可选）
    bd_ok, bd_norm, is_minor = _validate_birth_date(form["birth_date"])
    if not bd_ok:
        return render_template_string(REGISTER_HTML, cities=CITIES_REGISTRATION, grades=GRADES_REGISTRATION, error=bd_norm, form=form)
    form["birth_date"] = bd_norm

    # 微信 / 手机 二选一 或 都不填（学而思图 1 模式允许）
    if form["wechat_openid"] and form["phone"]:
        return render_template_string(REGISTER_HTML, cities=CITIES_REGISTRATION, grades=GRADES_REGISTRATION, error="微信扫码 + 手机号请二选一（避免重复绑定）", form=form)

    # 14 岁以下 + 无手机号兜底 → 拒绝
    if is_minor and not form["phone"]:
        return render_template_string(REGISTER_HTML, cities=CITIES_REGISTRATION, grades=GRADES_REGISTRATION, error="14 岁以下学员必须填写家长手机号（v3.5.2 PIPL §5.2 强制）", form=form)

    # ---- 去重 ----
    existing = _admin_students.get_student_by_uid(form["luogu_uid"])
    if existing:
        return render_template_string(
            REGISTER_HTML,
            cities=CITIES_REGISTRATION,
            grades=GRADES_REGISTRATION,
            error=f"洛谷 UID {form['luogu_uid']} 已注册（学员 id={existing['id']}），如需修改请联系教练",
            form=form,
        )

    # ---- 写入 ----
    note = f"v3.5.2 自助注册 IP={request.remote_addr or '—'}"
    if form["wechat_openid"]:
        note += f" · wechat={form['wechat_openid'][:8]}***"
    if form["phone"]:
        note += f" · phone={form['phone'][:3]}***{form['phone'][-2:] if len(form['phone']) >= 5 else ''}"
    if is_minor:
        note += " · MINOR=1"

    try:
        sid = _admin_students.create_student(
            luogu_uid=form["luogu_uid"],
            real_name=form["real_name"],
            grade=form["grade"],
            city=form["city"],
            gender=form["gender"],
            birth_date=form["birth_date"] or None,
            is_minor=is_minor,
            registered_via="self_web" if not form["wechat_openid"] else "wechat",
            note=note,
        )
    except Exception as e:  # noqa: BLE001
        return render_template_string(REGISTER_HTML, cities=CITIES_REGISTRATION, grades=GRADES_REGISTRATION, error=f"注册失败：{e}", form=form)

    flash(f"✅ 学员 {form['real_name']} 注册成功（id={sid}）")
    return redirect(url_for("student_me", luogu_uid=form["luogu_uid"]))


@app.route("/me/<luogu_uid>")
def student_me(luogu_uid: str):
    """v3.5.2 学员 Pro 自助面板（无密码，仅凭 luogu_uid 进入）

    简化模式：v3.5.2 暂用 luogu_uid 直链（家长端 token 同款模式）。
    未来 v3.5.3 接微信扫码/手机 OTP 后改为带签名 token。
    """
    student = _admin_students.get_student_by_uid(luogu_uid)
    if not student:
        return render_template_string(REGISTER_INVALID_HTML, message=f"洛谷 UID {luogu_uid} 未注册"), 404
    progress = _admin_students.get_student_gesp_progress(int(student["id"])) or {}
    # v3.5.2: 解析 city 所在省份 + grade 中文 label
    student_dict = dict(student)
    student_dict["province"] = _city_to_province(student_dict.get("city"))
    student_dict["grade_label"] = _grade_to_label(student_dict.get("grade"))
    # v3.5.2: 检查家长订阅状态（决定 AI 讲题是否可用）
    # activation_codes 表字段：code, sku, student_id, redeemed_at, expires_at
    # sku 实际值：parent_sub / popularize_camp / improve_camp
    has_parent_sub = False
    try:
        from task_store import _get_conn
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM activation_codes ac "
                "JOIN students s ON s.id = ac.student_id "
                "WHERE ac.sku = 'parent_sub' AND s.luogu_uid = ? "
                "AND ac.redeemed_at IS NOT NULL "
                "AND (ac.expires_at IS NULL OR ac.expires_at > datetime('now'))",
                (str(luogu_uid).strip(),),
            ).fetchone()
        finally:
            conn.close()
        has_parent_sub = bool(row and dict(row).get("n", 0) > 0)
    except Exception:
        has_parent_sub = False
    return render_template_string(
        STUDENT_ME_HTML,
        student=student_dict,
        progress=progress or {},
        has_parent_sub=has_parent_sub,
        token=luogu_uid,
        award_summary=_admin_students.get_student_award_summary(int(student["id"])) or {},
        csp_award_types=_admin_students.CSP_AWARD_TYPES,
        csp_award_levels=_admin_students.CSP_AWARD_LEVELS,
    )


# ---- v3.5.3 学员 GESP/CSP/NOIP/NOI 自录入 ----

@app.route("/me/<luogu_uid>/record-gesp", methods=["POST"])
def student_me_record_gesp(luogu_uid: str):
    """学员自录 GESP 真考成绩（4 字段：level / score / award_year / certificate_no）

    流程：找 competitions 中匹配的 gesp 赛事（按 level + year）→ UPSERT
    """
    student = _admin_students.get_student_by_uid(luogu_uid)
    if not student:
        return render_template_string(REGISTER_INVALID_HTML, message=f"洛谷 UID {luogu_uid} 未注册"), 404

    level_raw = (request.form.get("level") or "").strip()
    score_raw = (request.form.get("score") or "").strip()
    year_raw = (request.form.get("award_year") or "").strip()
    cert = (request.form.get("certificate_no") or "").strip()

    # 校验
    if not level_raw.isdigit() or not (1 <= int(level_raw) <= 8):
        flash("GESP 等级必须在 1-8")
        return redirect(url_for("student_me", luogu_uid=luogu_uid))
    if not score_raw.isdigit() or not (0 <= int(score_raw) <= 100):
        flash("分数必须在 0-100")
        return redirect(url_for("student_me", luogu_uid=luogu_uid))
    this_year = date.today().year
    if not year_raw.isdigit() or not (2015 <= int(year_raw) <= this_year + 1):
        flash(f"获奖年份必须在 2015-{this_year + 1}")
        return redirect(url_for("student_me", luogu_uid=luogu_uid))
    level = int(level_raw)
    score = int(score_raw)
    year = int(year_raw)

    # 找 competitions 表里的 GESP 赛事（按 level + year 推断 code）
    # 简化：code 形如 GESP-{year}-L{level}-{month}
    # v3.5.3 直接找 code LIKE '%L7-8%' 的赛事（覆盖该 level）
    from task_store import _get_conn
    conn = _get_conn()
    try:
        # 找最近的 GESP 赛事（4 次/年：3/6/9/12 月）
        # 按 data_year + exam_date 推断月份
        exam_id = None
        if level >= 7:
            code_pattern = "L7-8"
        else:
            code_pattern = f"L1-6"  # 1-6 级共用一个赛事
        row = conn.execute(
            "SELECT id FROM competitions WHERE type='gesp' AND code LIKE ? "
            "AND data_year = ? ORDER BY exam_date DESC LIMIT 1",
            (f"%{code_pattern}%", year),
        ).fetchone()
        if row:
            exam_id = int(row["id"])
        else:
            # 找不到 → fallback 找最近的 gesp 赛事
            row = conn.execute(
                "SELECT id FROM competitions WHERE type='gesp' ORDER BY exam_date DESC LIMIT 1"
            ).fetchone()
            if row:
                exam_id = int(row["id"])
            else:
                flash("系统暂无 GESP 赛事数据，请先跑 import_competitions.py")
                return redirect(url_for("student_me", luogu_uid=luogu_uid))
    finally:
        conn.close()

    try:
        _admin_students.add_gesp_exam(
            student_id=int(student["id"]),
            exam_id=exam_id,
            registered_level=level,
            actual_score=score,
            certificate_no=cert or None,
            recorded_by="self",
            award_year=year,
        )
        flash(f"✅ GESP {level} 级 {year} 年 {score} 分 已录入")
    except Exception as e:  # noqa: BLE001
        flash(f"⚠️ GESP 录入失败：{e}")
    return redirect(url_for("student_me", luogu_uid=luogu_uid))


@app.route("/me/<luogu_uid>/record-csp", methods=["POST"])
def student_me_record_csp(luogu_uid: str):
    """学员自录 CSP/NOIP/NOI 奖项（5 字段：competition_type/award_level/award_year/score/province）"""
    student = _admin_students.get_student_by_uid(luogu_uid)
    if not student:
        return render_template_string(REGISTER_INVALID_HTML, message=f"洛谷 UID {luogu_uid} 未注册"), 404

    ctype = (request.form.get("competition_type") or "").strip()
    level = (request.form.get("award_level") or "").strip()
    year_raw = (request.form.get("award_year") or "").strip()
    score_raw = (request.form.get("actual_score") or "").strip()
    province = (request.form.get("province") or "").strip()

    valid_types = {t[0] for t in _admin_students.CSP_AWARD_TYPES}
    valid_levels = {l[0] for l in _admin_students.CSP_AWARD_LEVELS}
    if ctype not in valid_types:
        flash("比赛类型不合法")
        return redirect(url_for("student_me", luogu_uid=luogu_uid))
    if level not in valid_levels:
        flash("奖项等级不合法")
        return redirect(url_for("student_me", luogu_uid=luogu_uid))
    this_year = date.today().year
    if not year_raw.isdigit() or not (2015 <= int(year_raw) <= this_year + 1):
        flash(f"获奖年份必须在 2015-{this_year + 1}")
        return redirect(url_for("student_me", luogu_uid=luogu_uid))
    score = int(score_raw) if score_raw.isdigit() else None

    try:
        _admin_students.add_csp_award(
            student_id=int(student["id"]),
            competition_type=ctype,
            award_level=level,
            award_year=int(year_raw),
            actual_score=score,
            province=province or None,
            recorded_by="self",
        )
        # 取可读 label
        type_label = next((t[1] for t in _admin_students.CSP_AWARD_TYPES if t[0] == ctype), ctype)
        level_label = next((l[1] for l in _admin_students.CSP_AWARD_LEVELS if l[0] == level), level)
        flash(f"✅ {type_label} {level_label} {year_raw} 已录入")
    except Exception as e:  # noqa: BLE001
        flash(f"⚠️ CSP 录入失败：{e}")
    return redirect(url_for("student_me", luogu_uid=luogu_uid))


@app.route("/me/<luogu_uid>/delete-csp/<int:award_id>", methods=["POST"])
def student_me_delete_csp(luogu_uid: str, award_id: int):
    """学员删除自录的 CSP 奖项（仅本人可删）"""
    student = _admin_students.get_student_by_uid(luogu_uid)
    if not student:
        return render_template_string(REGISTER_INVALID_HTML, message=f"洛谷 UID {luogu_uid} 未注册"), 404
    ok = _admin_students.delete_csp_award(int(award_id), int(student["id"]))
    if ok:
        flash("✅ 已删除")
    else:
        flash("⚠️ 删除失败（无权限或不存在）")
    return redirect(url_for("student_me", luogu_uid=luogu_uid))


# ---- v3.5.2 模板 ----

REGISTER_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>学员注册 · v3.5.2</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { font-family: -appleSystem, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif; }
        .reg-shadow { box-shadow: 0 8px 32px rgba(0,0,0,0.08); }
    </style>
</head>
<body class="min-h-screen flex items-center justify-center p-4 bg-gradient-to-br from-green-50 via-white to-emerald-50">
    <div class="w-full max-w-md bg-white rounded-2xl reg-shadow p-7">
        <div class="text-center mb-5">
            <div class="inline-block px-3 py-1 bg-green-100 text-green-700 text-xs rounded-full mb-2">v3.5.2</div>
            <h1 class="text-xl font-bold text-gray-800 mb-1">学员注册</h1>
            <p class="text-xs text-gray-500">学而思图 1 模式 · 4 字段极简</p>
        </div>

        {% if error %}
        <div class="mb-4 px-3 py-2 bg-red-50 border border-red-200 text-red-700 rounded-lg text-sm">⚠️ {{ error }}</div>
        {% endif %}

        <form method="POST" class="space-y-3">
            <div>
                <label class="block text-sm font-medium text-gray-700 mb-1"><span class="text-red-500">*</span> 城市</label>
                <select name="city" required class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-green-500 focus:border-green-500">
                    <option value="">请选择城市</option>
                    {% for group, items in cities %}
                    <optgroup label="📍 {{ group }}">
                        {% for c in items %}
                        <option value="{{ c }}" {% if form.city == c %}selected{% endif %}>{{ c }}</option>
                        {% endfor %}
                    </optgroup>
                    {% endfor %}
                </select>
            </div>

            <div>
                <label class="block text-sm font-medium text-gray-700 mb-1"><span class="text-red-500">*</span> 姓名</label>
                <input type="text" name="real_name" required maxlength="20"
                       value="{{ form.real_name or '' }}"
                       placeholder="请输入姓名"
                       class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-green-500 focus:border-green-500">
            </div>

            <div>
                <label class="block text-sm font-medium text-gray-700 mb-1"><span class="text-red-500">*</span> 年级</label>
                <select name="grade" required class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-green-500 focus:border-green-500">
                    <option value="">请选择年级</option>
                    {% for g_val, g_label in grades %}
                    <option value="{{ g_val }}" {% if form.grade == g_val %}selected{% endif %}>{{ g_label }}</option>
                    {% endfor %}
                </select>
            </div>

            <div>
                <label class="block text-sm font-medium text-gray-700 mb-1"><span class="text-red-500">*</span> 性别</label>
                <div class="grid grid-cols-2 gap-2">
                    <label class="flex items-center justify-center gap-2 border rounded-lg px-3 py-2 cursor-pointer hover:bg-green-50 {% if form.gender == 'M' %}bg-green-50 border-green-500{% endif %}">
                        <input type="radio" name="gender" value="M" {% if form.gender == 'M' %}checked{% endif %} class="text-green-600">
                        <span>♂ 男生</span>
                    </label>
                    <label class="flex items-center justify-center gap-2 border rounded-lg px-3 py-2 cursor-pointer hover:bg-pink-50 {% if form.gender == 'F' %}bg-pink-50 border-pink-500{% endif %}">
                        <input type="radio" name="gender" value="F" {% if form.gender == 'F' %}checked{% endif %} class="text-pink-600">
                        <span>♀ 女生</span>
                    </label>
                </div>
            </div>

            <div class="border-t border-gray-200 pt-3 mt-3">
                <p class="text-xs text-gray-500 mb-2">🔐 实名信息（任选其一，借力主站认证）</p>

                <div>
                    <label class="block text-sm font-medium text-gray-700 mb-1"><span class="text-red-500">*</span> 洛谷 UID</label>
                    <input type="text" name="luogu_uid" required pattern="[0-9]{6,10}"
                           value="{{ form.luogu_uid or '' }}"
                           placeholder="6-10 位数字"
                           class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-green-500 focus:border-green-500">
                    <p class="text-xs text-gray-400 mt-1">v3.5.2 借力洛谷主站实名 · 学员档案主键</p>
                </div>

                <div class="grid grid-cols-2 gap-2 mt-2">
                    <div>
                        <label class="block text-xs text-gray-600 mb-1">微信扫码（可选）</label>
                        <button type="button" onclick="document.getElementById('wechat_openid').value='demo_wx_openid_' + Math.random().toString(36).slice(2,10); this.textContent='✓ 已扫码';" class="w-full bg-green-500 text-white text-xs px-2 py-2 rounded-lg hover:bg-green-600">
                            🟢 微信扫码
                        </button>
                        <input type="hidden" name="wechat_openid" id="wechat_openid" value="">
                        <p class="text-xs text-gray-400 mt-1">v3.5.2 demo 桩</p>
                    </div>
                    <div>
                        <label class="block text-xs text-gray-600 mb-1">手机号（14 岁以下必填）</label>
                        <input type="tel" name="phone" pattern="1[3-9][0-9]{9}"
                               value="{{ form.phone or '' }}"
                               placeholder="11 位手机号"
                               class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-green-500 focus:border-green-500">
                    </div>
                </div>

                <div class="mt-2">
                    <label class="block text-xs text-gray-600 mb-1">出生日期（可选 · 用于 CSP 年龄判定）</label>
                    <input type="date" name="birth_date"
                           value="{{ form.birth_date or '' }}"
                           class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-green-500 focus:border-green-500">
                </div>
            </div>

            <div class="flex items-start gap-2 pt-2">
                <input type="checkbox" name="agree" id="agree" required
                       {% if form.agree %}checked{% endif %}
                       class="mt-1">
                <label for="agree" class="text-xs text-gray-600">
                    已阅读并同意 <a href="#" class="text-green-600 underline">《用户协议》</a>
                    和 <a href="#" class="text-green-600 underline">《未成年人个人信息保护知情同意书》</a>
                    （PIPL §5.2 · 14 岁以下需监护人陪同）
                </label>
            </div>

            <button type="submit" class="w-full bg-gradient-to-r from-green-600 to-emerald-600 text-white font-bold py-3 rounded-lg hover:from-green-700 hover:to-emerald-700 transition">
                完成
            </button>
        </form>

        <div class="text-center mt-4">
            <a href="/me/999105" class="text-xs text-gray-400 hover:text-gray-600">→ 体验已注册学员 /me/999105</a>
        </div>
    </div>
</body>
</html>
"""


STUDENT_ME_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>学员 Pro · {{ student.real_name or ('UID-' + student.luogu_uid) }}</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-50 min-h-screen">
    <div class="bg-gradient-to-r from-green-700 to-emerald-700 text-white">
        <div class="max-w-3xl mx-auto p-6">
            <h1 class="text-2xl font-bold mb-1">🎓 学员 Pro · v3.5.2</h1>
            <p class="text-sm opacity-90">欢迎，<strong>{{ student.real_name or ('UID-' + student.luogu_uid) }}</strong></p>
            <p class="text-xs opacity-75 mt-1">
                UID {{ student.luogu_uid }}
                · {{ student.province or '' }} {{ student.city or '城市未填' }}
                · {% if student.gender == 'M' %}男生{% elif student.gender == 'F' %}女生{% else %}性别未填{% endif %}
                · 年级 {{ student.grade_label or student.grade or '—' }}
                · 注册渠道 {{ student.registered_via or 'admin' }}
            </p>
        </div>
    </div>

    <div class="max-w-3xl mx-auto p-4 -mt-4">
        {% if progress and progress.progress_bar %}
        <div class="bg-white rounded-2xl shadow p-5 mb-4">
            <h2 class="text-lg font-bold text-gray-800 mb-3">🏆 我的 GESP 段位</h2>
            <div class="font-mono bg-gray-50 p-3 rounded text-sm overflow-x-auto">{{ progress.progress_bar }}</div>
            <p class="text-sm text-gray-600 mt-3">
                最近真考: {% if progress.student.gesp_latest_score is not none %}{{ progress.student.gesp_latest_score }} 分{% else %}暂无 · 等待教练录入 GESP 真考成绩{% endif %}
                · 下次可报: GESP {{ progress.next_eligible_level or 1 }} 级
            </p>
            {% if progress.can_exempt_csp_s %}
            <div class="mt-3 px-3 py-2 bg-purple-100 text-purple-800 rounded text-sm">🎁 已解锁 CSP-J + CSP-S 双免初赛</div>
            {% elif progress.can_exempt_csp_j %}
            <div class="mt-3 px-3 py-2 bg-green-100 text-green-800 rounded text-sm">🎁 已解锁 CSP-J 免初赛</div>
            {% else %}
            <div class="mt-3 px-3 py-2 bg-gray-100 text-gray-600 rounded text-sm">尚未解锁免初赛</div>
            {% endif %}
        </div>
        {% else %}
        <div class="bg-white rounded-2xl shadow p-5 mb-4 text-center">
            <p class="text-gray-500">📋 还没有 GESP 段位数据</p>
            <p class="text-xs text-gray-400 mt-1">请联系您的教练录入首次 GESP 真考成绩</p>
        </div>
        {% endif %}

        <!-- v3.5.3 学员 GESP/CSP/NOIP/NOI 自录入区 -->
        <div class="bg-white rounded-2xl shadow p-5 mb-4">
            <h2 class="text-lg font-bold text-gray-800 mb-3">📥 自录历史奖项</h2>
            <p class="text-xs text-gray-500 mb-4">家长/学员可自录入 GESP 真考 + CSP/NOIP/NOI 奖项，自动计算段位 + 免初赛</p>

            <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                <!-- GESP 真考录入 -->
                <form method="POST" action="/me/{{ token }}/record-gesp" class="bg-green-50 border border-green-200 rounded-lg p-3">
                    <h3 class="text-sm font-bold text-green-800 mb-2">🎯 GESP 真考（CCF 1-8 级）</h3>
                    <div class="grid grid-cols-2 gap-2">
                        <div>
                            <label class="text-xs text-gray-600">等级 (1-8)</label>
                            <select name="level" required class="w-full border rounded px-2 py-1 text-sm">
                                <option value="">选</option>
                                {% for n in range(1, 9) %}
                                <option value="{{ n }}">{{ n }} 级</option>
                                {% endfor %}
                            </select>
                        </div>
                        <div>
                            <label class="text-xs text-gray-600">分数 (0-100)</label>
                            <input type="number" name="score" min="0" max="100" required class="w-full border rounded px-2 py-1 text-sm" placeholder="如 85">
                        </div>
                        <div>
                            <label class="text-xs text-gray-600">年份</label>
                            <input type="number" name="award_year" min="2015" max="2030" required class="w-full border rounded px-2 py-1 text-sm" value="{{ 2024 }}">
                        </div>
                        <div>
                            <label class="text-xs text-gray-600">证书编号（可选）</label>
                            <input type="text" name="certificate_no" class="w-full border rounded px-2 py-1 text-sm" placeholder="GESP-...">
                        </div>
                    </div>
                    <button type="submit" class="mt-2 w-full bg-green-600 text-white text-xs font-bold py-1.5 rounded hover:bg-green-700">📥 录入 GESP 真考</button>
                </form>

                <!-- CSP/NOIP/NOI 录入 -->
                <form method="POST" action="/me/{{ token }}/record-csp" class="bg-blue-50 border border-blue-200 rounded-lg p-3">
                    <h3 class="text-sm font-bold text-blue-800 mb-2">🏅 CSP / NOIP / NOI</h3>
                    <div class="grid grid-cols-2 gap-2">
                        <div class="col-span-2">
                            <label class="text-xs text-gray-600">比赛类型</label>
                            <select name="competition_type" required class="w-full border rounded px-2 py-1 text-sm">
                                <option value="">选</option>
                                {% for code, label in csp_award_types %}
                                <option value="{{ code }}">{{ label }}</option>
                                {% endfor %}
                            </select>
                        </div>
                        <div>
                            <label class="text-xs text-gray-600">奖项</label>
                            <select name="award_level" required class="w-full border rounded px-2 py-1 text-sm">
                                <option value="">选</option>
                                {% for code, label in csp_award_levels %}
                                <option value="{{ code }}">{{ label }}</option>
                                {% endfor %}
                            </select>
                        </div>
                        <div>
                            <label class="text-xs text-gray-600">年份</label>
                            <input type="number" name="award_year" min="2015" max="2030" required class="w-full border rounded px-2 py-1 text-sm" value="{{ 2024 }}">
                        </div>
                        <div>
                            <label class="text-xs text-gray-600">分数（可选）</label>
                            <input type="number" name="actual_score" min="0" max="600" class="w-full border rounded px-2 py-1 text-sm" placeholder="如 235">
                        </div>
                        <div>
                            <label class="text-xs text-gray-600">省份（省赛）</label>
                            <input type="text" name="province" class="w-full border rounded px-2 py-1 text-sm" placeholder="如 浙江">
                        </div>
                    </div>
                    <button type="submit" class="mt-2 w-full bg-blue-600 text-white text-xs font-bold py-1.5 rounded hover:bg-blue-700">📥 录入 CSP/NOIP/NOI 奖项</button>
                </form>
            </div>

            <!-- 已录入列表 -->
            {% if award_summary and award_summary.total_awards and award_summary.total_awards > 0 %}
            <div class="mt-4 border-t border-gray-200 pt-3">
                <h3 class="text-sm font-bold text-gray-700 mb-2">📋 已录入奖项（{{ award_summary.total_awards }} 条）</h3>
                <div class="space-y-1.5 max-h-48 overflow-y-auto">
                    {% for a in award_summary.raw %}
                    <div class="flex items-center justify-between bg-gray-50 border border-gray-200 rounded px-3 py-1.5 text-xs">
                        <div>
                            {% set tlabel = csp_award_types|selectattr(0, 'equalto', a.competition_type)|first %}
                            {% set llabel = csp_award_levels|selectattr(0, 'equalto', a.award_level)|first %}
                            <span class="font-semibold text-gray-800">{{ tlabel[1] if tlabel else a.competition_type }}</span>
                            <span class="ml-1 px-1.5 py-0.5 bg-amber-100 text-amber-800 rounded">{{ llabel[1] if llabel else a.award_level }}</span>
                            <span class="ml-2 text-gray-500">{{ a.award_year }} 年</span>
                            {% if a.actual_score %}<span class="ml-2 text-gray-500">分 {{ a.actual_score }}</span>{% endif %}
                            {% if a.province %}<span class="ml-2 text-gray-500">{{ a.province }}</span>{% endif %}
                        </div>
                        <form method="POST" action="/me/{{ token }}/delete-csp/{{ a.id }}" class="inline">
                            <button type="submit" onclick="return confirm('确认删除此奖项？')" class="text-red-500 hover:text-red-700 text-xs">🗑</button>
                        </form>
                    </div>
                    {% endfor %}
                </div>
            </div>
            {% endif %}
        </div>

        <div class="{% if has_parent_sub %}bg-gradient-to-r from-blue-50 to-indigo-50 border border-blue-200{% else %}bg-gradient-to-r from-amber-50 to-orange-50 border border-amber-200{% endif %} rounded-2xl shadow p-5 mb-4">
            <h2 class="text-lg font-bold text-gray-800 mb-2">
                {% if has_parent_sub %}
                💎 家长订阅已激活 · AI 讲题可用
                {% else %}
                💬 AI 讲题 · 需家长订阅
                {% endif %}
            </h2>
            <ul class="text-sm text-gray-700 space-y-1 mb-3">
                <li>{% if has_parent_sub %}✅{% else %}☑️{% endif %} 段位图（自动展示历次 GESP 真考）</li>
                <li>{% if has_parent_sub %}✅{% else %}☑️{% endif %} 错题本（每周班级共性错题 + 自己的）</li>
                <li>
                    {% if has_parent_sub %}
                    ✅ <strong class="text-blue-700">StudyMate AI 讲题（已解锁）</strong>
                    {% else %}
                    🔒 StudyMate AI 讲题 → <strong class="text-amber-700">需家长加 V 兑换码</strong>
                    {% endif %}
                </li>
                <li>{% if has_parent_sub %}✅{% else %}☑️{% endif %} 倒推路径（强基 5 校 / CSP-J/S 倒计时）</li>
            </ul>
            {% if has_parent_sub %}
            <p class="text-xs text-blue-700">🎁 家长已订阅 · AI 讲题无限制 · v3.5.2</p>
            {% else %}
            <div class="bg-white border border-amber-200 rounded-lg p-3 mt-2">
                <p class="text-xs text-gray-700 mb-2">💡 家长加 V 兑换 <code class="font-mono">PARENT-SUB-XXXX</code> 后，<strong>AI 讲题自动解锁</strong>。这是"家长为孩子买"的家庭订阅模式。</p>
                <a href="/redeem" class="inline-block text-xs px-3 py-1.5 bg-amber-500 text-white rounded hover:bg-amber-600">🎁 兑换家长订阅码</a>
                <a href="/" class="inline-block text-xs px-3 py-1.5 border border-gray-300 text-gray-700 rounded hover:bg-gray-50 ml-1">加 V 获取 →</a>
            </div>
            {% endif %}
        </div>

        <div class="text-center text-xs text-gray-400 mt-6 mb-4">
            v3.5.2 学员 Pro 自助入口 · 基于洛谷 UID 直链（无密码模式）<br>
            真实部署时将改为微信扫码 / 短信 OTP（v3.5.3）
        </div>
    </div>
</body>
</html>
"""


REGISTER_INVALID_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>学员未注册</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-50 min-h-screen flex items-center justify-center p-6">
    <div class="bg-white rounded-2xl shadow p-8 max-w-md w-full text-center">
        <h1 class="text-2xl font-bold text-amber-700 mb-3">⚠️ {{ message }}</h1>
        <p class="text-gray-600 mb-4">请先完成注册</p>
        <a href="/register" class="inline-block bg-green-600 text-white px-6 py-2 rounded-lg hover:bg-green-700">去注册</a>
    </div>
</body>
</html>
"""


# ============================================================
# v3.5 Phase 2 · 家长端 + 学员目标 + 周报 + 跳级决策树
# ============================================================


@app.route("/parent", methods=["GET", "POST"])
def parent_panel_entry():
    """家长端入口页：v3.5.2（输入 token 跳转）"""
    error = None
    token = ""
    if request.method == "POST":
        token = (request.form.get("token") or "").strip()
        if not token or not token.replace("-", "").replace("_", "").isalnum() or len(token) < 8:
            error = "家长 token 无效（应为 8 位以上字母数字）"
        else:
            g = _admin_guardians.get_guardian_by_token(token)
            if not g:
                error = "家长 token 未找到，请向教练索取正确链接"
            else:
                return redirect(url_for("parent_panel_index", token=token))
    return render_template_string(
        PARENT_TOKEN_ENTRY_HTML,
        error=error,
        token=token,
    )


@app.route("/parent/<token>")
def parent_panel_index(token: str):
    """家长无登录面板首页：v3.5.1 学而思图 2 样式
    赛事仪表盘 + 倒推路径 + 段位 + 4 SKU 付费 CTA + CSP 年龄卡"""
    g = _admin_guardians.get_guardian_by_token(token)
    if not g:
        return render_template_string(PARENT_TOKEN_INVALID_HTML, message="家长链接无效或已过期"), 410
    student = _admin_students.get_student(int(g["student_id"]))
    if not student:
        return render_template_string(PARENT_TOKEN_INVALID_HTML, message="学员档案不存在"), 404
    progress = _admin_students.get_student_gesp_progress(int(g["student_id"])) or {}
    goal = _admin_goals.get_student_goal(int(g["student_id"])) or {}
    rec = _admin_goals.recommend_skip_path(int(g["student_id"]))
    reports = _weekly_reports.list_weekly_reports(int(g["student_id"]), limit=10)
    # v3.5.1: 出生日期按 grade 推断（CSP 12 岁门槛所需）
    # demo 学员 grade 缺省 2024 → 推断 2014-05-01（CSP 2026 刚好满足）
    from docs.gesp_estimator import is_csp_age_eligible
    # v3.5.3: 优先用学员真实 birth_date，没有再兜底
    student_dict = dict(student)
    real_birth = student_dict.get("birth_date")
    inferred_birth = real_birth or "2014-05-01"  # v3.5.3 demo 兜底；正式需 admin 录入
    age_j2026 = is_csp_age_eligible(inferred_birth, 2026)
    age_j2027 = is_csp_age_eligible(inferred_birth, 2027)
    # 政策水印
    try:
        from camp_curriculum import get_policy_events_last_updated
        policy_last_updated = get_policy_events_last_updated() or "—"
    except Exception:
        policy_last_updated = "—"
    # v3.5.2: 政策匹配学校库（家长版核心模块）
    from task_store import match_school_for_student
    # grade 字段原始值（PRIMARY_3/JUNIOR_2 等）已在 student 中
    policy_match = match_school_for_student(student_dict)
    # v3.5.3: 学员画像（年龄/省份/学段/GESP 视角 + 奖项 summary）
    profile = _admin_students.compute_student_profile(int(g["student_id"]))
    # age 算成"完成周岁"，避免 .5 显示
    from datetime import date as _date
    if profile.get("age") is not None:
        profile["age_label"] = f"{profile['age']} 岁"
    else:
        profile["age_label"] = "未填"
    profile["stage_label"] = {
        "primary": "小学",
        "junior": "初中",
        "senior": "高中",
    }.get(profile.get("stage"), "高中")  # v3.5.4: NOI 不面向大学生，删除"大学"分支
    rec_stage = profile.get("stage_recommendation") or {}
    return render_template_string(
        PARENT_PANEL_HTML,
        guardian=g,
        student=student,
        progress=progress or {},
        goal=goal,
        rec=rec,
        reports=reports,
        token=token,
        age_j2026=age_j2026,
        age_j2027=age_j2027,
        policy_last_updated=policy_last_updated,
        policy_match=policy_match,
        # v3.5.3 学员画像
        profile=profile,
        rec_stage=rec_stage,
        award_summary=profile.get("award_summary") or {},
    )


@app.route("/parent/<token>/report/<int:report_id>")
def parent_panel_report(token: str, report_id: int):
    """家长查看单份周报（HTML）+ 打开数 +1"""
    g = _admin_guardians.get_guardian_by_token(token)
    if not g:
        return render_template_string(PARENT_TOKEN_INVALID_HTML, message="家长链接无效或已过期"), 410
    # 校验周报所属学员与 token 匹配（防止横向越权）
    from task_store import _get_conn
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM weekly_reports WHERE id = ? AND student_id = ?",
            (int(report_id), int(g["student_id"])),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return render_template_string(PARENT_TOKEN_INVALID_HTML, message="周报不存在"), 404
    _admin_guardians.increment_weekly_report_open(int(report_id))
    # 直接输出 HTML 文件
    html_path = ROOT / dict(row)["html_path"]
    if not html_path.exists():
        return render_template_string(PARENT_TOKEN_INVALID_HTML, message="周报文件已丢失"), 410
    return send_file(str(html_path), mimetype="text/html")


# ---- Phase 2 模板 ----

ADMIN_STUDENTS_GUARDIANS_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>家长列表 - {{ student.real_name or ('UID-' + student.luogu_uid) }}</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 min-h-screen p-6">
    <div class="max-w-4xl mx-auto">
        <div class="flex items-center justify-between mb-6">
            <h1 class="text-3xl font-bold text-blue-900">👨‍👩‍👧 家长列表</h1>
            <div class="flex items-center gap-4">
                <a href="/admin/students/{{ student.id }}" class="text-blue-600 hover:underline">← 返回学员</a>
                <a href="/admin/students" class="text-gray-600 hover:underline">学员列表</a>
            </div>
        </div>
        <div class="bg-white rounded-xl shadow p-6 mb-4">
            <p class="text-gray-600">学员：<strong>{{ student.real_name or ('UID-' + student.luogu_uid) }}</strong>
            · UID <code class="text-xs">{{ student.luogu_uid }}</code> · 学校 {{ student.school or '—' }}</p>
        </div>
        {% if notice %}
        <div class="mb-4 rounded-lg border px-4 py-3 text-sm {% if notice_type == 'error' %}bg-red-50 border-red-200 text-red-700{% else %}bg-green-50 border-green-200 text-green-700{% endif %}">
            <pre class="whitespace-pre-wrap font-sans">{{ notice }}</pre>
        </div>
        {% endif %}
        {% if error %}
        <div class="mb-4 rounded-lg border bg-red-50 border-red-200 text-red-700 px-4 py-3 text-sm">{{ error }}</div>
        {% endif %}

        <div class="bg-white rounded-xl shadow p-6 mb-6">
            <h2 class="text-lg font-semibold text-gray-800 mb-4">➕ 添加家长</h2>
            <form method="POST" class="space-y-4">
                <div class="grid grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-700 mb-1">显示名称（爸/妈/监护人）</label>
                        <input type="text" name="display_name" class="w-full border rounded-lg px-3 py-2" placeholder="如：张妈妈">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-700 mb-1">通知渠道</label>
                        <select name="notify_channel" class="w-full border rounded-lg px-3 py-2">
                            <option value="email">邮件（email）</option>
                            <option value="sms">短信（sms）</option>
                            <option value="wechat">微信（wechat）</option>
                            <option value="none">不通知</option>
                        </select>
                    </div>
                </div>
                <div class="grid grid-cols-2 gap-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-700 mb-1">手机</label>
                        <input type="text" name="phone" class="w-full border rounded-lg px-3 py-2" placeholder="11 位手机号（可选）">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-700 mb-1">邮箱</label>
                        <input type="email" name="email" class="w-full border rounded-lg px-3 py-2" placeholder="parent@example.com（可选）">
                    </div>
                </div>
                <p class="text-xs text-gray-500">⚠️ 添加即视为已获 PIPL §5.2 同意（IP {{ request.remote_addr or '—' }}），token 30 天后过期。</p>
                <button type="submit" class="bg-blue-600 text-white px-6 py-2 rounded-lg hover:bg-blue-700">添加并生成 token</button>
            </form>
        </div>

        <div class="bg-white rounded-xl shadow overflow-hidden">
            <div class="px-6 py-4 border-b border-gray-200">
                <h2 class="text-lg font-semibold text-gray-800">已绑定家长（{{ guardians|length }}）</h2>
            </div>
            {% if guardians %}
            <table class="min-w-full text-sm text-left">
                <thead class="bg-gray-50 text-gray-600 font-medium">
                    <tr>
                        <th class="px-6 py-3">ID</th>
                        <th class="px-6 py-3">显示名</th>
                        <th class="px-6 py-3">手机 / 邮箱</th>
                        <th class="px-6 py-3">通知</th>
                        <th class="px-6 py-3">Token</th>
                        <th class="px-6 py-3">过期</th>
                        <th class="px-6 py-3">操作</th>
                    </tr>
                </thead>
                <tbody>
                {% for g in guardians %}
                    <tr class="border-t border-gray-100">
                        <td class="px-6 py-3 text-gray-500">#{{ g.id }}</td>
                        <td class="px-6 py-3">{{ g.display_name or '—' }}</td>
                        <td class="px-6 py-3 text-xs text-gray-600">
                            {{ g.phone or '' }}{% if g.phone and g.email %} · {% endif %}{{ g.email or '' }}
                        </td>
                        <td class="px-6 py-3">{{ g.notify_channel }}</td>
                        <td class="px-6 py-3 font-mono text-xs">{{ g.notify_token[:12] + '...' if g.notify_token else '—' }}</td>
                        <td class="px-6 py-3 text-xs text-gray-500">{{ g.notify_token_expires_at or '—' }}</td>
                        <td class="px-6 py-3">
                            <a href="/parent/{{ g.notify_token }}" target="_blank" class="text-blue-600 hover:underline mr-2">预览</a>
                            <form method="POST" action="/admin/students/{{ student.id }}/guardians/{{ g.id }}/rotate" class="inline">
                                <button type="submit" class="text-orange-600 hover:underline">重置</button>
                            </form>
                            <form method="POST" action="/admin/students/{{ student.id }}/guardians/{{ g.id }}/delete" class="inline" onsubmit="return confirm('确认删除家长 #{{ g.id }}？');">
                                <button type="submit" class="text-red-600 hover:underline ml-2">删除</button>
                            </form>
                        </td>
                    </tr>
                {% endfor %}
                </tbody>
            </table>
            {% else %}
            <div class="p-8 text-center text-gray-400">尚未绑定家长</div>
            {% endif %}
        </div>
    </div>
</body>
</html>
"""


ADMIN_STUDENTS_GOAL_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>学员目标 - {{ student.real_name or ('UID-' + student.luogu_uid) }}</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 min-h-screen p-6">
    <div class="max-w-3xl mx-auto">
        <div class="flex items-center justify-between mb-6">
            <h1 class="text-3xl font-bold text-blue-900">🎯 学员目标路径</h1>
            <a href="/admin/students/{{ student.id }}" class="text-blue-600 hover:underline">← 返回学员</a>
        </div>
        {% if notice %}
        <div class="mb-4 rounded-lg border px-4 py-3 text-sm {% if notice_type == 'error' %}bg-red-50 border-red-200 text-red-700{% else %}bg-green-50 border-green-200 text-green-700{% endif %}">
            {{ notice }}
        </div>
        {% endif %}
        <div class="bg-white rounded-xl shadow p-6 mb-4">
            <p class="text-gray-600">学员：<strong>{{ student.real_name or ('UID-' + student.luogu_uid) }}</strong>
            · UID <code class="text-xs">{{ student.luogu_uid }}</code></p>
        </div>

        <form method="POST" class="bg-white rounded-xl shadow p-6 space-y-4 mb-6">
            <div>
                <label class="block text-sm font-medium text-gray-700 mb-1">主路径</label>
                <select name="primary_path" class="w-full border rounded-lg px-3 py-2">
                    {% for p in primary_paths %}
                    <option value="{{ p }}" {% if goal.primary_path == p %}selected{% endif %}>{{ p }}</option>
                    {% endfor %}
                </select>
            </div>
            <div class="grid grid-cols-2 gap-4">
                <div>
                    <label class="block text-sm font-medium text-gray-700 mb-1">目标大学</label>
                    <select name="target_university" class="w-full border rounded-lg px-3 py-2">
                        <option value="">— 未指定 —</option>
                        {% for u in sample_universities %}
                        <option value="{{ u }}" {% if goal.target_university == u %}selected{% endif %}>{{ u }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div>
                    <label class="block text-sm font-medium text-gray-700 mb-1">目标省份</label>
                    <input type="text" name="target_province" value="{{ goal.target_province or '' }}"
                           class="w-full border rounded-lg px-3 py-2" placeholder="如：北京">
                </div>
            </div>
            <div>
                <label class="block text-sm font-medium text-gray-700 mb-1">备注</label>
                <textarea name="notes" rows="2" class="w-full border rounded-lg px-3 py-2">{{ goal.notes or '' }}</textarea>
            </div>
            <button type="submit" class="bg-blue-600 text-white px-6 py-2 rounded-lg hover:bg-blue-700">保存目标</button>
        </form>

        {% if rec and rec.next_eligible_level %}
        <div class="bg-white rounded-xl shadow p-6">
            <h2 class="text-lg font-semibold text-gray-800 mb-3">🤖 AI 跳级建议</h2>
            <div class="bg-blue-50 border border-blue-200 rounded-lg p-4 text-sm">
                <p class="mb-2"><strong>主路径：</strong>{{ rec.primary_path }}</p>
                {% if rec.current_level %}<p class="mb-2"><strong>当前等级：</strong>GESP {{ rec.current_level }} 级{% if rec.last_score %} · 最近 {{ rec.last_score }} 分{% endif %}</p>{% endif %}
                <p class="mb-2"><strong>下次可报：</strong>GESP {{ rec.next_eligible_level }} 级</p>
                <p class="mb-2"><strong>推荐：</strong><span class="text-blue-700 font-bold">{{ rec.recommendation }}</span></p>
                <p class="text-gray-600">{{ rec.reasoning }}</p>
            </div>
        </div>
        {% endif %}
    </div>
</body>
</html>
"""


ADMIN_STUDENTS_REPORTS_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>周报列表 - {{ student.real_name or ('UID-' + student.luogu_uid) }}</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 min-h-screen p-6">
    <div class="max-w-4xl mx-auto">
        <div class="flex items-center justify-between mb-6">
            <h1 class="text-3xl font-bold text-blue-900">📊 家长周报</h1>
            <div class="flex items-center gap-4">
                <a href="/admin/students/{{ student.id }}" class="text-blue-600 hover:underline">← 返回学员</a>
                <form method="POST" action="/admin/students/{{ student.id }}/reports/generate" class="inline">
                    <button type="submit" class="bg-blue-600 text-white px-4 py-2 rounded-lg hover:bg-blue-700">⚡ 立即生成本周报</button>
                </form>
            </div>
        </div>
        {% if notice %}
        <div class="mb-4 rounded-lg border px-4 py-3 text-sm {% if notice_type == 'error' %}bg-red-50 border-red-200 text-red-700{% else %}bg-green-50 border-green-200 text-green-700{% endif %}">{{ notice }}</div>
        {% endif %}
        <div class="bg-white rounded-xl shadow p-6 mb-4">
            <p class="text-gray-600">学员：<strong>{{ student.real_name or ('UID-' + student.luogu_uid) }}</strong>
            · GESP 最高 {{ student.gesp_highest_passed or 0 }} 级
            · 下次可报 GESP {{ student.gesp_next_eligible_level or 1 }} 级</p>
        </div>
        <div class="bg-white rounded-xl shadow overflow-hidden">
            <div class="px-6 py-4 border-b border-gray-200">
                <h2 class="text-lg font-semibold text-gray-800">历史周报（{{ reports|length }}）</h2>
            </div>
            {% if reports %}
            <table class="min-w-full text-sm text-left">
                <thead class="bg-gray-50 text-gray-600 font-medium">
                    <tr>
                        <th class="px-6 py-3">ID</th>
                        <th class="px-6 py-3">周开始</th>
                        <th class="px-6 py-3">送达时间</th>
                        <th class="px-6 py-3">打开数</th>
                        <th class="px-6 py-3">操作</th>
                    </tr>
                </thead>
                <tbody>
                {% for r in reports %}
                    <tr class="border-t border-gray-100">
                        <td class="px-6 py-3 text-gray-500">#{{ r.id }}</td>
                        <td class="px-6 py-3">{{ r.week_start }}</td>
                        <td class="px-6 py-3 text-xs text-gray-500">{{ r.delivered_at or '—' }}</td>
                        <td class="px-6 py-3">{{ r.open_count or 0 }}</td>
                        <td class="px-6 py-3">
                            <a href="/admin/students/{{ student.id }}/reports/{{ r.id }}" target="_blank" class="text-blue-600 hover:underline">查看</a>
                        </td>
                    </tr>
                {% endfor %}
                </tbody>
            </table>
            {% else %}
            <div class="p-8 text-center text-gray-400">尚未生成周报 · 点上方"立即生成本周报"开始</div>
            {% endif %}
        </div>
    </div>
</body>
</html>
"""


PARENT_PANEL_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>家长端 · 赛事仪表盘 - {{ student.real_name or ('UID-' + student.luogu_uid) }}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif; }
        .card-shadow { box-shadow: 0 4px 16px rgba(0,0,0,0.06); }
    </style>
</head>
<body class="bg-gray-50 min-h-screen">
    <!-- 顶部 Banner（学而思图 2 风格：双 CTA + 强基倒计时） -->
    <div class="bg-gradient-to-r from-blue-900 via-indigo-700 to-purple-700 text-white">
        <div class="max-w-3xl mx-auto p-6">
            <div class="flex items-center justify-between mb-3">
                <h1 class="text-2xl font-bold">专业赛考规划 · 助力科特长生</h1>
                <span class="text-xs opacity-75">v3.5.1</span>
            </div>
            <p class="text-sm opacity-90 mb-1">
                学员：<strong>{{ student.real_name or ('UID-' + student.luogu_uid) }}</strong>
                · 入学年份 {{ student.grade or '—' }}
                · 通知渠道 {{ guardian.notify_channel }}
            </p>
            <p class="text-xs opacity-75">
                您是 <strong>{{ guardian.display_name or '已绑定家长' }}</strong> · token 有效期至 {{ guardian.notify_token_expires_at }}
            </p>
        </div>
    </div>

    <div class="max-w-3xl mx-auto p-4 -mt-4">

        <!-- v3.5.3 学员画像卡（年龄/城市/学段 + GESP 路径建议 · 替代 v3.5.1 CSP 12 岁卡） -->
        <div class="bg-white rounded-2xl card-shadow p-5 mb-4 border-l-4 {% if rec_stage.csp_visible %}border-emerald-500{% else %}border-blue-500{% endif %}">
            <div class="flex items-start justify-between mb-3">
                <div>
                    <h2 class="text-base font-bold text-gray-800 mb-1">👤 学员画像</h2>
                    <p class="text-xs text-gray-500">
                        {% if profile.real_name %}<strong>{{ profile.real_name }}</strong>{% else %}UID-{{ student.luogu_uid }}{% endif %}
                        · {{ profile.age_label }}
                        · {{ profile.province or '未填' }} {{ student.city or '' }}
                        · <span class="px-1.5 py-0.5 {% if profile.stage == 'primary' %}bg-blue-100 text-blue-700{% elif profile.stage == 'junior' %}bg-emerald-100 text-emerald-700{% else %}bg-purple-100 text-purple-700{% endif %} rounded">{{ profile.stage_label }}</span>
                    </p>
                </div>
                <span class="text-xs px-2 py-0.5 bg-emerald-100 text-emerald-700 rounded-full">v3.5.3</span>
            </div>

            <!-- 学段 GESP 视角 / 路径建议 -->
            <div class="bg-{% if rec_stage.csp_visible %}emerald{% else %}blue{% endif %}-50 border border-{% if rec_stage.csp_visible %}emerald{% else %}blue{% endif %}-200 rounded-lg p-3 mb-3">
                <div class="text-sm font-bold text-{% if rec_stage.csp_visible %}emerald{% else %}blue{% endif %}-800 mb-1">
                    🎯 {{ rec_stage.perspective or 'GESP 路径' }}
                </div>
                <p class="text-xs text-gray-700 leading-relaxed">{{ rec_stage.summary or '—' }}</p>
                {% if rec_stage.next_exam %}
                <div class="mt-2 text-xs text-{% if rec_stage.csp_visible %}emerald{% else %}blue{% endif %}-700">
                    📅 <strong>下一步：</strong>{{ rec_stage.next_exam }}
                </div>
                {% endif %}
                {% if rec_stage.pitfalls %}
                <ul class="mt-2 space-y-1">
                    {% for p in rec_stage.pitfalls %}
                    <li class="text-xs text-amber-700">⚠️ {{ p }}</li>
                    {% endfor %}
                </ul>
                {% endif %}
            </div>

            <!-- 已有奖项 summary -->
            <div class="grid grid-cols-3 gap-2 text-center">
                <div class="bg-gray-50 rounded p-2">
                    <div class="text-xs text-gray-500">已录入奖项</div>
                    <div class="text-base font-bold text-gray-800">{{ award_summary.total_awards or 0 }}</div>
                </div>
                <div class="bg-gray-50 rounded p-2">
                    <div class="text-xs text-gray-500">最高奖项</div>
                    <div class="text-xs font-bold text-amber-700 mt-1">{{ award_summary.best_label or '无' }}</div>
                </div>
                <div class="bg-gray-50 rounded p-2">
                    <div class="text-xs text-gray-500">CSP 年龄</div>
                    <div class="text-xs font-bold mt-1">
                        {% if age_j2026.eligible %}<span class="text-green-600">26 满足</span>{% else %}<span class="text-amber-600">26 待满</span>{% endif %}
                        ·
                        {% if age_j2027.eligible %}<span class="text-green-600">27 满足</span>{% else %}<span class="text-amber-600">27 待满</span>{% endif %}
                    </div>
                </div>
            </div>

            <p class="text-xs text-gray-400 mt-2">📌 家长可在「<a href="/me/{{ student.luogu_uid }}" class="text-blue-600 hover:underline">学员自助 /me/{{ student.luogu_uid }}</a>」中补录 GESP 真考 + CSP/NOIP/NOI 奖项</p>
        </div>

        <!-- v3.5.2 政策匹配学校库（家长版核心 · 地域+学段→升学路径） -->
        <div class="bg-white rounded-2xl card-shadow p-5 mb-4 border-l-4 border-emerald-500">
            <div class="flex items-start justify-between mb-3">
                <div>
                    <h2 class="text-base font-bold text-gray-800">
                        🏫 升学路径匹配
                        <span class="text-xs text-gray-500 font-normal">（基于 {{ student.city or '城市未填' }} · {{ policy_match.stage_label or student.grade or '—' }}）</span>
                    </h2>
                    <p class="text-xs text-gray-500 mt-1">
                        {% if policy_match.matches %}
                        当前匹配 <strong class="text-emerald-700">{{ policy_match.match_type_label }}</strong>，共 <strong>{{ policy_match.matches|length }}</strong> 所样板学校
                        {% else %}
                        暂无可匹配升学路径（请检查城市/年级是否填写）
                        {% endif %}
                    </p>
                </div>
                <span class="text-xs px-2 py-0.5 bg-emerald-100 text-emerald-700 rounded-full">v3.5.2</span>
            </div>

            {% if policy_match.matches %}
            <div class="space-y-2">
                {% for m in policy_match.matches %}
                <div class="border {% if m.is_recommended %}border-emerald-300 bg-emerald-50/30{% else %}border-gray-200{% endif %} rounded-lg p-3">
                    <div class="flex items-start justify-between">
                        <div class="flex-1">
                            <div class="font-bold text-sm text-gray-800">
                                {{ loop.index }}. {{ m.school_name }}
                                {% if m.is_recommended %}
                                <span class="text-xs px-1.5 py-0.5 bg-emerald-500 text-white rounded ml-1">⭐ 推荐</span>
                                {% endif %}
                            </div>
                            <div class="text-xs text-gray-600 mt-1">
                                📋 {{ m.policy_summary }}
                            </div>
                            <div class="flex gap-3 mt-1.5 text-xs text-gray-500">
                                <span>👥 招生 {{ m.enrollment_count or '—' }} 人</span>
                                <span>🎯 {{ m.requires_competition or '—' }}</span>
                                <span>📍 {{ m.city }}{% if m.city != m.province %} · {{ m.province }}{% endif %}</span>
                            </div>
                        </div>
                        {% if m.policy_url %}
                        <a href="{{ m.policy_url }}" target="_blank" rel="noopener" class="text-xs text-emerald-600 hover:underline whitespace-nowrap">查看政策 →</a>
                        {% endif %}
                    </div>
                </div>
                {% endfor %}
            </div>
            <p class="text-xs text-gray-400 mt-3">
                💡 数据为样板，v3.5 反向 Scope 限制：仅 6 城中小学/6 城自招高中 + 强基 5 校。
                升学政策每年可能调整，请以教育局官方简章为准。
            </p>
            {% else %}
            <div class="text-xs text-gray-400 py-2">
                {% if policy_match.stage == 'college' or policy_match.stage == 'graduated' %}
                ✅ 学员已毕业，升学匹配结束
                {% elif policy_match.stage == 'unknown' %}
                ⚠️ 学段未识别，请确认 {{ student.grade or '—' }} 字段
                {% else %}
                ⚠️ 当前城市（{{ student.city or '未填' }}）暂无匹配数据
                {% endif %}
            </div>
            {% endif %}
        </div>

        <!-- 双 CTA 卡（学而思图 2 左下：课程政策 + 考试赛事） -->
        <div class="grid grid-cols-2 gap-3 mb-4">
            <a href="#course-policy" class="bg-white rounded-2xl card-shadow p-5 flex items-center gap-3 hover:shadow-lg transition">
                <div class="w-12 h-12 rounded-xl bg-gradient-to-br from-blue-500 to-blue-600 flex items-center justify-center text-white text-2xl">📘</div>
                <div>
                    <div class="text-base font-bold text-gray-800">课程政策</div>
                    <div class="text-xs text-gray-500 mt-1">退课 / 转课 / 续费</div>
                </div>
            </a>
            <a href="#competition-path" class="bg-white rounded-2xl card-shadow p-5 flex items-center gap-3 hover:shadow-lg transition">
                <div class="w-12 h-12 rounded-xl bg-gradient-to-br from-orange-500 to-red-500 flex items-center justify-center text-white text-2xl">🏆</div>
                <div>
                    <div class="text-base font-bold text-gray-800">考试赛事</div>
                    <div class="text-xs text-gray-500 mt-1">GESP / CSP / 强基</div>
                </div>
            </a>
        </div>

        <!-- 赛事路径规划（学而思图 2 中段：图标 + 名称 + 一句话价值主张 + CTA） -->
        <div id="competition-path" class="bg-white rounded-2xl card-shadow p-5 mb-4">
            <h2 class="text-lg font-bold text-gray-800 mb-3">📅 赛事路径规划</h2>
            <div class="space-y-3">
                <!-- GESP -->
                <div class="flex items-center justify-between p-3 bg-green-50 rounded-xl">
                    <div class="flex items-center gap-3">
                        <div class="w-10 h-10 rounded-lg bg-green-600 flex items-center justify-center text-white font-bold">G</div>
                        <div>
                            <div class="font-bold text-gray-800">GESP 1-8 级</div>
                            <div class="text-xs text-gray-600">CCF 编程能力等级认证 · 一年 4 次（3/6/9/12 月）</div>
                        </div>
                    </div>
                    {% if progress and progress.progress_bar %}
                    <div class="text-right">
                        <div class="font-mono text-xs text-gray-700">{{ progress.progress_bar }}</div>
                        <div class="text-xs text-gray-500 mt-1">下次可报：{{ progress.next_eligible_level or 1 }} 级</div>
                    </div>
                    {% endif %}
                </div>
                <!-- CSP-J -->
                <div class="flex items-center justify-between p-3 bg-blue-50 rounded-xl">
                    <div class="flex items-center gap-3">
                        <div class="w-10 h-10 rounded-lg bg-blue-600 flex items-center justify-center text-white font-bold">J</div>
                        <div>
                            <div class="font-bold text-gray-800">CSP-J 入门级</div>
                            <div class="text-xs text-gray-600">CCF 非专业级软件能力认证 · 9 月初赛 + 10 月复赛</div>
                            {% if progress and progress.can_exempt_csp_j %}
                            <div class="text-xs text-green-600 font-semibold mt-1">🎁 GESP 7 级 80+ 已解锁免初赛</div>
                            {% endif %}
                        </div>
                    </div>
                    <span class="text-xs text-gray-400">每年 9/10 月</span>
                </div>
                <!-- CSP-S -->
                <div class="flex items-center justify-between p-3 bg-indigo-50 rounded-xl">
                    <div class="flex items-center gap-3">
                        <div class="w-10 h-10 rounded-lg bg-indigo-600 flex items-center justify-center text-white font-bold">S</div>
                        <div>
                            <div class="font-bold text-gray-800">CSP-S 提高级</div>
                            <div class="text-xs text-gray-600">CCF 非专业级软件能力认证 · 难度高于 J</div>
                            {% if progress and progress.can_exempt_csp_s %}
                            <div class="text-xs text-green-600 font-semibold mt-1">🎁 GESP 8 级 80+ 已解锁免初赛</div>
                            {% endif %}
                        </div>
                    </div>
                    <span class="text-xs text-gray-400">每年 9/10 月</span>
                </div>
                <!-- 强基 5 校 -->
                <div class="flex items-center justify-between p-3 bg-purple-50 rounded-xl">
                    <div class="flex items-center gap-3">
                        <div class="w-10 h-10 rounded-lg bg-purple-600 flex items-center justify-center text-white text-lg">🏛️</div>
                        <div>
                            <div class="font-bold text-gray-800">强基计划 5 校样板</div>
                            <div class="text-xs text-gray-600">清北复交浙 · 高考后 6 月校测 · 高考占 85% + 校测 15%</div>
                        </div>
                    </div>
                    <span class="text-xs text-gray-400">每年 3-6 月</span>
                </div>
            </div>
        </div>

        <!-- AI 跳级建议 -->
        {% if rec and rec.next_eligible_level %}
        <div class="bg-gradient-to-r from-blue-50 to-indigo-50 rounded-2xl card-shadow p-5 mb-4 border border-blue-200">
            <h2 class="text-base font-bold text-gray-800 mb-2">🤖 AI 跳级建议</h2>
            <p class="text-sm text-gray-700 mb-1">主路径：<strong>{{ rec.primary_path }}</strong></p>
            <p class="text-sm mb-1">推荐：<span class="text-blue-700 font-bold">{{ rec.recommendation }}</span></p>
            <p class="text-xs text-gray-500">{{ rec.reasoning }}</p>
        </div>
        {% endif %}

        <!-- 白名单赛事（教育部 2024 修订） -->
        <div class="bg-white rounded-2xl card-shadow p-5 mb-4">
            <h2 class="text-lg font-bold text-gray-800 mb-3">🏛️ 白名单赛事（教育部 2024 修订）</h2>
            <p class="text-xs text-gray-500 mb-3">教育部公布的 2024-2026 学年面向中小学生的全国性竞赛活动名单 · 信息学相关条目：</p>
            <div class="space-y-2 text-sm">
                <div class="flex items-center justify-between p-2 hover:bg-gray-50 rounded">
                    <span>📌 CCF 非专业级软件能力认证（CSP-J/S）</span>
                    <span class="text-xs text-green-600 font-semibold">✅ 主办 CCF</span>
                </div>
                <div class="flex items-center justify-between p-2 hover:bg-gray-50 rounded">
                    <span>📌 全国青少年信息学奥林匹克竞赛（NOI）</span>
                    <span class="text-xs text-green-600 font-semibold">✅ 主办 CCF</span>
                </div>
                <div class="flex items-center justify-between p-2 hover:bg-gray-50 rounded">
                    <span>📌 全国中学生信息学奥林匹克联赛（NOIP）</span>
                    <span class="text-xs text-green-600 font-semibold">✅ 主办 CCF</span>
                </div>
            </div>
            <p class="text-xs text-gray-400 mt-3">v3.5.1：v3.5 §8 反向 Scope 禁"CCF 规则解读 / 强基 39 校全数据"，故仅列名不解读</p>
        </div>

        <!-- 4 SKU 付费 CTA（v3.5.1 转化入口） -->
        <div class="bg-gradient-to-r from-amber-50 to-orange-50 rounded-2xl card-shadow p-5 mb-4 border border-amber-200">
            <h2 class="text-lg font-bold text-gray-800 mb-3">💎 4 SKU 升级路径</h2>
            <div class="grid grid-cols-2 gap-3 text-sm">
                <div class="bg-white rounded-xl p-3 border-2 border-gray-200">
                    <div class="font-bold text-gray-800">学员 Pro</div>
                    <div class="text-2xl font-bold text-amber-600 my-1">¥15<span class="text-xs text-gray-500">/月</span></div>
                    <div class="text-xs text-gray-600">段位图 · 错题本 · StudyMate</div>
                </div>
                <div class="bg-white rounded-xl p-3 border-2 border-gray-200">
                    <div class="font-bold text-gray-800">家长订阅</div>
                    <div class="text-2xl font-bold text-amber-600 my-1">¥30<span class="text-xs text-gray-500">/月</span></div>
                    <div class="text-xs text-gray-600">周报 · 倒推 · 政策水印</div>
                </div>
                <div class="bg-white rounded-xl p-3 border-2 border-blue-300">
                    <div class="font-bold text-gray-800">普及组冲刺营</div>
                    <div class="text-2xl font-bold text-blue-600 my-1">¥99<span class="text-xs text-gray-500">/4 周</span></div>
                    <div class="text-xs text-gray-600">GESP 7 级 80+ → CSP-J 免初赛</div>
                </div>
                <div class="bg-white rounded-xl p-3 border-2 border-purple-300">
                    <div class="font-bold text-gray-800">提高组冲刺营</div>
                    <div class="text-2xl font-bold text-purple-600 my-1">¥299<span class="text-xs text-gray-500">/8 周</span></div>
                    <div class="text-xs text-gray-600">GESP 8 级 80+ → CSP-S 免初赛</div>
                </div>
            </div>
            <p class="text-xs text-gray-500 mt-3 text-center">📮 兑换码请向您的教练索取 · 当前页面（家长订阅 ¥30/月）已包含</p>
        </div>

        <!-- 课程政策区块（学而思图 2 左下 CTA 落地） -->
        <div id="course-policy" class="bg-white rounded-2xl card-shadow p-5 mb-4">
            <h2 class="text-lg font-bold text-gray-800 mb-3">📘 课程政策</h2>
            <ul class="text-sm text-gray-700 space-y-2">
                <li class="flex items-start gap-2">
                    <span class="text-green-500 mt-0.5">✓</span>
                    <span><strong>退课</strong>：开课 7 天内可全额退，联系您的教练</span>
                </li>
                <li class="flex items-start gap-2">
                    <span class="text-green-500 mt-0.5">✓</span>
                    <span><strong>转课</strong>：同级任意时段可调，不限次数</span>
                </li>
                <li class="flex items-start gap-2">
                    <span class="text-green-500 mt-0.5">✓</span>
                    <span><strong>续费</strong>：到期前 7 天邮件 + 短信双提醒</span>
                </li>
                <li class="flex items-start gap-2">
                    <span class="text-green-500 mt-0.5">✓</span>
                    <span><strong>冲刺营达成</strong>：完成度 ≥ 90% + GESP 真考 80+ → 不达标触发退费建议</span>
                </li>
            </ul>
        </div>

        <!-- 周报列表 -->
        <div class="bg-white rounded-2xl card-shadow p-5 mb-4">
            <h2 class="text-lg font-bold text-gray-800 mb-3">📊 家长周报</h2>
            {% if reports %}
            <ul class="divide-y divide-gray-100">
                {% for r in reports %}
                <li class="py-3 flex items-center justify-between">
                    <a href="/parent/{{ token }}/report/{{ r.id }}" class="text-blue-600 hover:underline flex items-center gap-2">
                        <span class="text-lg">📄</span>
                        <span><strong>{{ r.week_start }}</strong> 周报</span>
                    </a>
                    <div class="text-right">
                        <div class="text-xs text-gray-500">已打开 {{ r.open_count or 0 }} 次</div>
                        <div class="text-xs text-gray-400">{{ r.delivered_at or '' }}</div>
                    </div>
                </li>
                {% endfor %}
            </ul>
            {% else %}
            <p class="text-gray-400 text-sm text-center py-4">尚未生成周报</p>
            {% endif %}
        </div>

        <!-- 段位卡（v3.5.1 放底部） -->
        {% if progress and progress.progress_bar %}
        <div class="bg-white rounded-2xl card-shadow p-5 mb-4">
            <h2 class="text-lg font-bold text-gray-800 mb-3">🏆 GESP 段位卡</h2>
            <div class="font-mono bg-gray-50 p-3 rounded text-sm overflow-x-auto">{{ progress.progress_bar }}</div>
            <p class="text-sm text-gray-600 mt-3">
                最近真考: {% if progress.student.gesp_latest_score is not none %}{{ progress.student.gesp_latest_score }} 分{% else %}无{% endif %}
                · 下次可报: GESP {{ progress.next_eligible_level or 1 }} 级
            </p>
            {% if progress.can_exempt_csp_s %}
            <div class="mt-3 px-3 py-2 bg-purple-100 text-purple-800 rounded text-sm">🎁 已解锁 CSP-J + CSP-S 双免初赛</div>
            {% elif progress.can_exempt_csp_j %}
            <div class="mt-3 px-3 py-2 bg-green-100 text-green-800 rounded text-sm">🎁 已解锁 CSP-J 免初赛</div>
            {% endif %}
        </div>
        {% endif %}

        <!-- 政策水印 + 免责 -->
        <div class="text-center text-xs text-gray-400 mt-6 mb-4 px-4">
            <p>📅 政策数据最后更新：{{ policy_last_updated }}（v3.5 §9 风险对冲：30 天未更新将显示水印）</p>
            <p class="mt-2">本页面基于脱敏数据 · 不含 PII · v3.5.1 家长订阅功能</p>
            <p class="mt-1">如有疑问请联系您的教练</p>
        </div>
    </div>
</body>
</html>
"""


PARENT_TOKEN_INVALID_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>家长链接无效</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 min-h-screen flex items-center justify-center p-6">
    <div class="bg-white rounded-xl shadow p-8 max-w-md w-full text-center">
        <h1 class="text-2xl font-bold text-red-700 mb-3">⚠️ 链接无效</h1>
        <p class="text-gray-600 mb-4">{{ message }}</p>
        <p class="text-xs text-gray-400">请联系您的教练重新获取家长链接</p>
    </div>
</body>
</html>
"""


PARENT_TOKEN_ENTRY_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>家长端入口 · 信竞 AI 报告</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body{font-family:-appleSystem,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;background:linear-gradient(135deg,#fef3c7 0%,#fef9c3 100%);min-height:100vh;}
    </style>
</head>
<body class="flex items-center justify-center p-4">
    <div class="bg-white rounded-2xl shadow-lg p-8 w-full max-w-md">
        <div class="text-center mb-5">
            <div class="inline-block px-3 py-1 bg-amber-100 text-amber-700 text-xs rounded-full mb-2">v3.5.2 · 家长端</div>
            <h1 class="text-2xl font-bold text-gray-800 mb-1">👨‍👩‍👧 家长端入口</h1>
            <p class="text-sm text-gray-500">输入教练给您的邀请码</p>
        </div>
        {% if error %}
        <div class="mb-4 px-3 py-2 bg-red-50 border border-red-200 text-red-700 rounded-lg text-sm">⚠️ {{ error }}</div>
        {% endif %}
        <form method="POST" class="space-y-3">
            <div>
                <label class="block text-sm font-medium text-gray-700 mb-1">家长邀请码</label>
                <input type="text" name="token" required minlength="8" maxlength="64"
                       value="{{ token or '' }}"
                       placeholder="如：abc123def456"
                       class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono focus:ring-2 focus:ring-amber-500 focus:border-amber-500">
                <p class="text-xs text-gray-400 mt-1">由教练 1v1 邀请分发 · 8 位以上字母数字</p>
            </div>
            <button type="submit" class="w-full bg-gradient-to-r from-amber-500 to-amber-600 text-white font-bold py-2.5 rounded-lg hover:from-amber-600 hover:to-amber-700 transition">
                进入家长面板
            </button>
        </form>

        <!-- 加 V 引导（v3.5.2 终态） -->
        <div class="mt-5 p-3 bg-amber-50 border border-amber-200 rounded-lg text-xs text-amber-800">
            <div class="font-bold mb-1">💬 没有邀请码？</div>
            <p class="mb-1">加客服微信号 <span class="font-mono font-semibold select-all">xinjing-ai-vip</span>，回复「家长」领取。</p>
            <p class="text-amber-700 text-xs">工作日 9:00-21:00 · 节假日 10:00-18:00</p>
        </div>

        <div class="text-center mt-4 text-xs text-gray-400">
            <a href="/" class="text-amber-600 hover:underline">返回首页</a> · 教练入口 <a href="/coach" class="text-indigo-600 hover:underline">/coach</a>
        </div>
    </div>
</body>
</html>
"""


# 把 ROOT 注入到 _weekly_reports（避免循环导入）
ROOT = _ROOT  # noqa: F821


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
