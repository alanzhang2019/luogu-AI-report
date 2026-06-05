import os
import json
import uuid
import threading
import time
import hmac
from pathlib import Path
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit
from openai import APIConnectionError, APITimeoutError, APIError, RateLimitError as OpenAIRateLimitError
try:
    from flask import Flask, render_template_string, request, redirect, url_for, send_from_directory, send_file, session
except ImportError:
    from flask import Flask, render_template_string, request, redirect, url_for, send_from_directory, send_file
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
    return stage_prefix + str(exc)


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
    <title>洛谷 AI 测评报告生成器</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        .app-body{background:#f3f4f6;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px;font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,"Noto Sans","PingFang SC","Microsoft YaHei",sans-serif;}
        .app-card{background:#fff;border-radius:16px;box-shadow:0 10px 25px rgba(0,0,0,.08);padding:32px;width:100%;max-width:560px;}
        .app-title{font-size:24px;font-weight:800;color:#1e3a8a;margin:0 0 6px;}
        .app-subtitle{color:#6b7280;margin:0 0 10px;font-size:14px;}
        .app-muted{color:#9ca3af;font-size:12px;margin:0 0 18px;}
        .app-box{border-radius:10px;padding:12px 12px;border:1px solid #e5e7eb;}
        .app-box-yellow{background:#fffbeb;border-color:#fde68a;color:#92400e;}
        .app-box-blue{background:#eff6ff;border-color:#bfdbfe;color:#1d4ed8;}
        .app-box-green{background:#ecfdf5;border-color:#a7f3d0;color:#065f46;}
        .app-box-red{background:#fef2f2;border-color:#fecaca;color:#991b1b;}
        .app-label{display:block;font-size:13px;font-weight:700;color:#374151;}
        .app-input{margin-top:6px;display:block;width:100%;border-radius:10px;border:1px solid #d1d5db;padding:10px 12px;box-shadow:0 1px 2px rgba(0,0,0,.04);}
        .app-input:focus{outline:none;border-color:#3b82f6;box-shadow:0 0 0 3px rgba(59,130,246,.2);}
        .app-btn{display:inline-flex;align-items:center;justify-content:center;width:100%;border-radius:10px;padding:10px 14px;font-weight:800;transition:all .15s ease;}
        .app-btn-primary{background:#2563eb;color:#fff;}
        .app-btn-primary:hover{background:#1d4ed8;}
        .app-btn-secondary{background:#fff;color:#1d4ed8;border:1px solid #93c5fd;}
        .app-btn-secondary:hover{background:#eff6ff;}
        .app-btn:disabled{opacity:.5;cursor:not-allowed;}
        .app-row{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
        .app-space{margin-bottom:14px;}
        .app-small{font-size:12px;opacity:.9;}
    </style>
</head>
<body class="app-body bg-gray-100 min-h-screen flex items-center justify-center p-4">
    <div class="app-card bg-white rounded-xl shadow-lg p-8 w-full max-w-lg">
        <h1 class="app-title text-2xl font-bold text-blue-900 mb-2">洛谷 AI 测评报告生成器</h1>
        <p class="app-subtitle text-gray-500 mb-2">输入洛谷 Cookies 与 OpenAI 配置，在线生成测评报告</p>
        <div class="app-muted text-xs text-gray-400 mb-6 flex items-center justify-between gap-2">
            <div>
                QQ交流群：<span id="qqGroup" class="text-blue-700 font-semibold select-all">610931699</span>
                <span class="text-gray-400">（复制即可）</span>
            </div>
            <button id="copyQqBtn" type="button" class="px-3 py-1 rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50">复制</button>
        </div>
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
                <input type="text" name="client_id" value="{{ form_values.client_id }}" required class="app-input mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-blue-500 focus:ring-blue-500 border p-2">
            </div>
            <div>
                <label class="app-label block text-sm font-medium text-gray-700">_uid</label>
                <input type="text" name="uid" value="{{ form_values.uid }}" required class="app-input mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-blue-500 focus:ring-blue-500 border p-2">
            </div>
            <div>
                <label class="app-label block text-sm font-medium text-gray-700">C3VK</label>
                <input type="text" name="c3vk" value="{{ form_values.c3vk }}" required class="app-input mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-blue-500 focus:ring-blue-500 border p-2">
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
                <input type="password" name="api_key" value="{{ form_values.api_key }}" class="app-input mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-blue-500 focus:ring-blue-500 border p-2">
                <p class="text-xs text-gray-500 mt-1">{{ server_key_hint }}</p>
            </div>
            <div>
                <label class="app-label block text-sm font-medium text-gray-700">API Base URL（留空使用服务端默认）</label>
                <input type="text" name="base_url" value="{{ form_values.base_url }}" class="app-input mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-blue-500 focus:ring-blue-500 border p-2">
            </div>
            <div>
                <label class="app-label block text-sm font-medium text-gray-700">模型名称（留空使用服务端默认）</label>
                <input type="text" name="model_name" value="{{ form_values.model_name }}" class="app-input mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-blue-500 focus:ring-blue-500 border p-2">
            </div>
            <div class="grid grid-cols-2 gap-4">
                <div>
                    <label class="app-label block text-sm font-medium text-gray-700">姓名</label>
                    <input type="text" name="student_name" value="{{ form_values.student_name }}" class="app-input mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-blue-500 focus:ring-blue-500 border p-2">
                </div>
                <div>
                    <label class="app-label block text-sm font-medium text-gray-700">学校</label>
                    <input type="text" name="school" value="{{ form_values.school }}" class="app-input mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-blue-500 focus:ring-blue-500 border p-2">
                </div>
            </div>
            <div>
                <label class="app-label block text-sm font-medium text-gray-700">年级</label>
                <input type="text" name="grade" value="{{ form_values.grade }}" class="app-input mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-blue-500 focus:ring-blue-500 border p-2">
            </div>
            <input type="hidden" name="max_passed" value="{{ form_values.max_passed }}">
            <input type="hidden" name="max_failed" value="{{ form_values.max_failed }}">
            <button id="generateBtn" type="submit" class="app-btn app-btn-primary w-full bg-blue-600 text-white font-semibold py-2 px-4 rounded-md hover:bg-blue-700 transition">生成报告</button>
        </form>
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
) -> None:
    current_stage = "生成 AI 报告"
    with TASKS_LOCK:
        update_task(
            task_id,
            stage=current_stage,
            ai_progress=1,
            ai_elapsed_seconds=0,
            message=f"正在调用 {model_name} 生成 AI 报告，请耐心等待...",
        )
    if not str(api_key or "").strip():
        raise ValueError("未配置 OpenAI API Key：请在页面填写 OpenAI API Key，或在服务端设置环境变量 OPENAI_API_KEY / OPENAI_ADMIN_KEY，并重启服务使其生效。")
    ai_holder: dict[str, object] = {
        "done": False,
        "report_md": None,
        "exc": None,
        "attempt": 1,
        "status_message": f"正在调用 {model_name} 生成 AI 报告，请耐心等待...",
    }

    def _run_ai():
        attempt = 1
        while attempt <= AI_GENERATION_MAX_RETRIES:
            ai_holder["attempt"] = attempt
            ai_holder["status_message"] = (
                f"正在调用 {model_name} 生成 AI 报告（第 {attempt}/{AI_GENERATION_MAX_RETRIES} 次）..."
            )
            try:
                ai_holder["report_md"] = generate_ai_report(export_data, api_key, base_url, model_name)
                ai_holder["exc"] = None
                break
            except Exception as exc:
                ai_holder["exc"] = exc
                if (attempt >= AI_GENERATION_MAX_RETRIES) or (not _is_retryable_ai_error(exc)):
                    break
                ai_holder["status_message"] = (
                    f"AI 接口暂时不可用，正在等待后自动重试（第 {attempt}/{AI_GENERATION_MAX_RETRIES} 次失败）：{exc}"
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
    with TASKS_LOCK:
        update_task(
            task_id,
            stage=current_stage,
            ai_progress=100,
            ai_elapsed_seconds=int(time.time() - ai_start),
            message=f"AI 报告已生成（{api_key_source}），正在写入文件...",
        )

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report_md)

    current_stage = "生成图表与 HTML/PDF"
    with TASKS_LOCK:
        update_task(task_id, stage=current_stage, message="正在生成图表与 HTML/PDF...")
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
            with TASKS_LOCK:
                update_task(
                    task_id,
                    stage=current_stage,
                    source_code_success=source_code_success,
                    source_code_total=source_code_total,
                    message=(
                        f"检测到任务 {resume_task_id[:8]} 已完成前置数据准备，"
                        "正在跳过洛谷抓取并直接续跑 AI 报告..."
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
        with TASKS_LOCK:
            update_task(task_id, message="正在补全题目标签数据...")
        current_stage = "补全题目标签"
        enrich_problem_tags(luogu, all_passed)

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

        for idx, problem in enumerate(pending_passed_problems):
            try:
                record = _pick_record_for_problem(
                    luogu=luogu,
                    uid=uid,
                    pid=problem.pid,
                    max_records_to_try=5,
                    require_source_code=True,
                    detail_fetch_state=detail_fetch_state,
                )
            except Exception as e:
                record = {"error": str(e)}
            save_cached_source_record(uid, problem.pid, record if isinstance(record, dict) else None)
            passed_items.append({"problem": problem.to_json(), "record": record})
            processed += 1
            if _is_source_code_present(record):
                source_code_success += 1
            _maybe_update_progress()

        for idx, problem in enumerate(pending_failed_problems):
            try:
                record = _pick_record_for_problem(
                    luogu=luogu,
                    uid=uid,
                    pid=problem.pid,
                    max_records_to_try=5,
                    require_source_code=True,
                    detail_fetch_state=detail_fetch_state,
                )
            except Exception as e:
                record = {"error": str(e)}
            save_cached_source_record(uid, problem.pid, record if isinstance(record, dict) else None)
            failed_items.append({"problem": problem.to_json(), "record": record})
            processed += 1
            if _is_source_code_present(record):
                source_code_success += 1
            _maybe_update_progress()

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
        with TASKS_LOCK:
            update_task(task_id, status="error", message=describe_generation_error(e, current_stage))
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
        </div>
        {% elif status == 'error' %}
        <a href="{{ retry_url }}" class="block w-full bg-blue-600 text-white font-semibold py-2 px-4 rounded-md hover:bg-blue-700 transition mt-4">返回重试</a>
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
    return render_template_string(
        STATUS_HTML,
        status=task.get("status", "unknown"),
        message=task.get("message", ""),
        stage=str(task.get("stage", "") or ""),
        source_code_success=int(task.get("source_code_success", 0) or 0),
        source_code_total=int(task.get("source_code_total", 0) or 0),
        ai_progress=int(task.get("ai_progress", 0) or 0),
        ai_elapsed_seconds=int(task.get("ai_elapsed_seconds", 0) or 0),
        html=task.get("html", ""),
        pdf=_download_report_url(pdf_url),
        md=task.get("md", ""),
        retry_url=url_for("retry_task", task_id=task_id),
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
