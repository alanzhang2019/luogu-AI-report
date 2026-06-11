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
    from flask import Flask, render_template_string, request, redirect, url_for, send_from_directory, send_file, session, flash, Response
except ImportError:
    from flask import Flask, render_template_string, request, redirect, url_for, send_from_directory, send_file, session, flash, Response
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
    normalize_report_markdown,
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
    _get_conn,
)


def _init_report_hides_table() -> None:
    """v3.7 · 幂等创建 report_hides 表（PDF 隐藏标记）。"""
    conn = _get_conn()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS report_hides (
            task_id     TEXT PRIMARY KEY,
            hide_pdf    INTEGER NOT NULL DEFAULT 1,
            hide_html   INTEGER NOT NULL DEFAULT 0,
            ref_uid     TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_report_hides_ref ON report_hides(ref_uid);
        """)
        conn.commit()
    finally:
        conn.close()


def _record_hide_pdf(task_id: str) -> None:
    """v3.7 · 报告生成后写入 hide_pdf=1 标记（不抛异常）。"""
    if not task_id:
        return
    try:
        _init_report_hides_table()
        conn = _get_conn()
        try:
            conn.execute(
                """INSERT INTO report_hides (task_id, hide_pdf, updated_at)
                   VALUES (?, 1, datetime('now'))
                   ON CONFLICT(task_id) DO UPDATE SET
                     hide_pdf=1, updated_at=datetime('now')""",
                (str(task_id).strip(),),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as _e:
        print(f"[v3.7] _record_hide_pdf warning: {_e}")


def _check_file_visibility(rel_path: str) -> tuple[bool, str]:
    """v3.7 · 检查 reports/<task_id>/* 文件是否对外可见。

    返回 (visible, reason)：
      - True, ""          → 可见
      - False, "PDF 暂未开放..." → 隐藏
    规则：
      - 非 reports/ 路径 → 全部 True
      - *.md 公开        → 全部 True
      - *.pdf 受 hide_pdf 控制
      - *.html 受 hide_html 控制（默认 0）
    DB 异常 → fail-open 返回 True。
    """
    if not rel_path or not rel_path.startswith("reports/"):
        return True, ""
    lower = rel_path.lower()
    if lower.endswith(".md"):
        return True, ""
    parts = rel_path.split("/")
    if len(parts) < 3:
        return True, ""
    task_id = parts[1]
    try:
        _init_report_hides_table()
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT hide_pdf, hide_html FROM report_hides WHERE task_id=?",
                (task_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return True, ""
        row = dict(row)
        if lower.endswith(".pdf") and row.get("hide_pdf", 0):
            return False, "PDF 暂未开放 · 请扫码海报查看在线版报告"
        if lower.endswith(".html") and row.get("hide_html", 0):
            return False, "HTML 暂未开放"
        return True, ""
    except Exception as _e:
        print(f"[v3.7] _check_file_visibility fail-open: {_e}")
        return True, ""


app = Flask(__name__)
app.secret_key = (
    os.environ.get("ADMIN_SESSION_SECRET")
    or os.environ.get("FLASK_SECRET_KEY")
    or "luogu-ai-report-admin-secret-change-me"
)

# v3.7 · report_hides 表初始化（幂等）
try:
    _init_report_hides_table()
except Exception as _e:
    print(f"[v3.7] report_hides init warning: {_e}")

# 任务状态锁（数据库操作线程安全）
TASKS_LOCK = threading.Lock()
REBUILD_TASKS_LOCK = threading.Lock()
REBUILD_TASKS: dict[str, dict[str, str]] = {}
ACTIVE_GENERATION_TASKS_LOCK = threading.Lock()
ACTIVE_GENERATION_TASKS: dict[str, threading.Thread] = {}
AI_GENERATION_MAX_RETRIES = 4
AI_GENERATION_RETRY_SLEEP_SECONDS = 12

# v3.5.2 传播期开关：先做用户基数，100+ 真学员后再揭幕付费
# 关闭办法：设环境变量 LUOGU_HIDE_COMMERCE=0，重启服务
_HIDE_COMMERCE = os.environ.get("LUOGU_HIDE_COMMERCE", "1").strip() not in ("0", "false", "False", "no", "off")


# v3.5.2 传播期"商业化暂不开放"页（GET/POST 通用）
COMMERCE_PAUSED_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>🚀 9 月传播期 · 商业化暂未开放 · 信竞 AI 报告</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
             background:linear-gradient(135deg,#eef2ff 0%,#fdf4ff 50%,#fff7ed 100%);min-height:100vh;}
        .pill{display:inline-block;padding:3px 10px;border-radius:9999px;font-size:12px;font-weight:600;}
    </style>
</head>
<body class="flex items-center justify-center p-4">
    <div class="bg-white rounded-2xl shadow-xl p-8 w-full max-w-2xl">
        <div class="text-center mb-5">
            <div class="pill bg-indigo-100 text-indigo-700 mb-2">v3.5.2 · 传播期模式</div>
            <h1 class="text-3xl font-bold text-gray-800 mb-2">🌱 先把基础用户跑起来</h1>
            <p class="text-sm text-gray-600">
                商业化（家长订阅 / 冲刺营）将在 <strong>100+ 真实学员</strong>之后再揭幕。
            </p>
        </div>

        <div class="space-y-3 mb-5">
            <div class="px-4 py-3 bg-emerald-50 border border-emerald-200 rounded-lg">
                <div class="font-bold text-emerald-700 mb-1">✅ 仍然可用（不收费）</div>
                <ul class="text-sm text-emerald-800 list-disc list-inside space-y-1">
                    <li>洛谷账号基础测评（学而思 v2 主功能）</li>
                    <li>学员自助中心 <code class="font-mono text-xs">/me/&lt;UID&gt;</code>（段位 + 错题）</li>
                    <li>9 月赛事日历 + 免初赛倒计时</li>
                    <li>GESP 跳级 4 规则试算（AI 估算）</li>
                </ul>
            </div>

            <div class="px-4 py-3 bg-amber-50 border border-amber-200 rounded-lg">
                <div class="font-bold text-amber-700 mb-1">⏸ 暂不开放</div>
                <ul class="text-sm text-amber-800 list-disc list-inside space-y-1">
                    <li>家长订阅版（v3.5.2 AI 二次生成）</li>
                    <li>普及组冲刺营（4 周 PJC-）</li>
                    <li>提高组冲刺营（8 周 IJC-）</li>
                </ul>
            </div>
        </div>

        <div class="text-center space-x-3">
            <a href="/" class="inline-block px-5 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700">
                ← 回到首页
            </a>
            <a href="/me" class="inline-block px-5 py-2 border border-gray-300 text-gray-700 text-sm rounded-lg hover:bg-gray-50">
                查询我的 UID
            </a>
        </div>

        <p class="text-xs text-gray-400 text-center mt-5">
            💡 关闭方式：<code class="font-mono">LUOGU_HIDE_COMMERCE=0</code> 重启即可恢复（仅教练/客服）
        </p>
    </div>
</body>
</html>
"""


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
    import traceback
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    print(f"[ERROR][{stage}] {base_detail}\n{tb}", flush=True)
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

    {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
    <div class="space-y-2">
        {% for category, message in messages %}
        <div class="app-box {% if category == 'warning' %}app-box-yellow{% elif category == 'error' %}app-box-red{% elif category == 'success' %}app-box-green{% else %}app-box-blue{% endif %}">
            {{ message }}
        </div>
        {% endfor %}
    </div>
    {% endif %}
    {% endwith %}

    <!-- 顶部品牌 -->
    <div class="app-card text-center">
        <h1 class="app-title">🏆 信竞 AI 报告 · 选手成长平台</h1>
        <div class="app-muted flex items-center justify-center gap-2">
            <span>QQ交流群：<span id="qqGroup" class="text-emerald-700 font-semibold select-all">610931699</span></span>
            <button id="copyQqBtn" type="button" class="px-2 py-0.5 rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50 text-xs">复制</button>
        </div>
    </div>

    <!-- v3.5.2 主 CTA · 统一"AI 生成学习报告"入口 -->
    <div class="app-card bg-gradient-to-r from-emerald-50 to-cyan-50 border-2 border-emerald-300">
        <div class="text-center mb-3">
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

        <!-- 学员 UID 快速入口（保留 · 用于老用户/已注册用户） -->
        <form id="me-entry" action="/me/0" method="get" class="mt-4 flex gap-2" onsubmit="event.preventDefault(); var u=document.getElementById('meUid').value.trim(); if(u && /^\d{6,10}$/.test(u)) window.location.href='/me/'+u; else alert('请输入 6-10 位洛谷 UID');">
            <input id="meUid" type="text" inputmode="numeric" pattern="\\d{6,10}" placeholder="洛谷 UID 6-10 位（已注册用户直接进入个人中心）" class="app-input flex-1">
            <button type="submit" class="app-btn app-btn-secondary px-4 whitespace-nowrap">进入</button>
        </form>
    </div>

    <!-- 3 大引擎（价值感知 · 替代价格表） · v3.6 暂时隐藏（已物理删除） -->

    <!-- 加 V 与客服（v3.5.2 唯一购买通道） · v3.5.2 传播期隐藏 -->
    {% if not commerce_hidden %}
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
    {% endif %}

    <!-- v3.6 首页精简：暂时隐藏"新用户说明"+"给新用户"+"底部"三块 -->

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
    """读出"返回重试"所需的表单快照

    优先级：
      1) task.retry_form_json（v3.5.2+ 写入，包含完整 11 字段）
      2) task 学生字段（student_name / school / grade）兜底
         → 老 task 没存 retry_form_json 时，至少回填这三个字段，
           让用户少填一遍，cookies 仍需补全
    """
    snapshot: dict[str, str] = {}
    if isinstance(task, dict):
        raw_json = str(task.get("retry_form_json", "") or "").strip()
        if raw_json:
            try:
                payload = json.loads(raw_json)
                if isinstance(payload, dict):
                    snapshot = build_retry_form_snapshot(payload)
            except Exception:
                snapshot = {}
        # 兜底：把 task 里能直接拿到的字段补上
        if not snapshot.get("student_name") and task.get("student_name"):
            snapshot["student_name"] = str(task.get("student_name") or "")
        if not snapshot.get("school") and task.get("school"):
            snapshot["school"] = str(task.get("school") or "")
        if not snapshot.get("grade") and task.get("grade"):
            snapshot["grade"] = str(task.get("grade") or "")
    return snapshot


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
        commerce_hidden=_HIDE_COMMERCE,
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


def _build_report_paths(task_id: str, student_name: str, luogu_uid: str = "") -> tuple[Path, Path, Path, Path, Path]:
    safe_name = "".join(c for c in student_name if c.isalnum() or c in "_-").strip() or "unknown"
    folder_name = f"{task_id[:8]}_{safe_name}"
    out_dir = Path("reports") / folder_name
    out_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = out_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    # v3.5.2 · 写 luogu_uid 侧车文件，方便后续按 UID 精确反查
    # （目录名用 task_id 命名，无法直接用 luogu_uid 命中）
    if luogu_uid:
        try:
            (out_dir / "luogu_uid.txt").write_text(str(luogu_uid).strip(), encoding="utf-8")
        except Exception:
            pass
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

    # 关键：AI 原始输出不含『知识树图谱』『掌握度判定标准』等结构化小节，
    # 统一在这里走一遍 normalize_report_markdown，由代码注入最新结构，
    # 避免依赖 prompt 命中 / 旧报告残留。
    if report_md and export_data is not None:
        try:
            report_md = normalize_report_markdown(report_md, export_data)
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(report_md)
        except Exception as _norm_err:
            log_message("WARN", f"normalize_report_markdown failed in main flow: {_norm_err}")

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

        out_dir, assets_dir, md_path, html_path, pdf_path = _build_report_paths(
            task_id, student_name, luogu_uid=str(form.get("luogu_uid", "") or "").strip()
        )

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
        {% if task_type == 'parent_subscribe' %}
        <div class="mb-4 text-left">
            <div class="flex items-center justify-between text-sm text-gray-600 mb-1">
                <span>📨 家长订阅版 AI 生成进度</span>
                <span class="font-semibold text-gray-800">{{ ai_progress }}%{% if ai_elapsed_seconds and ai_elapsed_seconds|int > 0 %} · {{ ai_elapsed_seconds }}s{% endif %}</span>
            </div>
            <div class="w-full bg-gray-200 rounded-full h-3 overflow-hidden">
                <div class="bg-gradient-to-r from-amber-400 to-rose-400 h-3" style="width: {{ ai_progress|int }}%;"></div>
            </div>
        </div>
        {% if status == 'done' %}
        <a href="{{ ps_html }}" target="_blank" class="block w-full bg-gradient-to-r from-amber-500 to-rose-500 text-white font-bold py-3 px-4 rounded-md hover:from-amber-600 hover:to-rose-600 transition mb-2">📨 查看家长订阅版（AI 真生成）</a>
        <a href="{{ ps_md }}" target="_blank" class="block w-full bg-gray-200 text-gray-800 font-semibold py-2 px-4 rounded-md hover:bg-gray-300 transition">查看 Markdown 原文</a>
        <a href="/me/{{ luogu_uid }}/parent-subscribe" class="block w-full bg-white text-amber-700 border border-amber-300 font-semibold py-2 px-4 rounded-md hover:bg-amber-50 transition mt-2">↩ 返回家长订阅版页</a>
        {% elif status == 'error' %}
        <a href="/me/{{ luogu_uid }}/parent-subscribe" class="block w-full bg-rose-500 text-white font-bold py-2 px-4 rounded-md hover:bg-rose-600 transition">返回重试</a>
        {% else %}
        <p class="text-sm text-gray-400">页面每 3 秒自动刷新，AI 正在基于您家孩子的报告重写一份家长视角的深度分析...</p>
        {% endif %}
        {% elif status == 'done' %}
        <div class="space-y-3">
            <a href="{{ html }}" target="_blank" class="block w-full bg-blue-600 text-white font-semibold py-2 px-4 rounded-md hover:bg-blue-700 transition">查看 HTML 报告</a>
            <a href="{{ pdf }}" target="_blank" class="block w-full bg-gray-700 text-white font-semibold py-2 px-4 rounded-md hover:bg-gray-800 transition">下载 PDF 报告</a>
            <a href="{{ md }}" target="_blank" class="block w-full bg-gray-200 text-gray-800 font-semibold py-2 px-4 rounded-md hover:bg-gray-300 transition">查看 Markdown 原文</a>
            {% if me_url %}
            {# v3.6 修复：点击"最终生成家长订阅版"按钮 → 直接 POST 触发生成（不再跳到 API key 表单页） #}
            <form method="POST" action="/me/{{ me_url.split('/')[-1] }}/start-parent-subscribe" class="block">
                <button type="submit" class="w-full bg-gradient-to-r from-amber-500 to-rose-500 text-white font-bold py-2.5 px-4 rounded-md hover:from-amber-600 hover:to-rose-600 transition">
                    📨 最终生成家长订阅版
                </button>
                <p class="text-[10px] text-gray-500 text-center mt-1">💡 使用服务端环境变量 OPENAI_API_KEY 直接生成（约 1-2 分钟）</p>
            </form>
            {% endif %}
        </div>
        {% elif status == 'error' %}
        <a href="{{ retry_url }}" class="block w-full bg-blue-600 text-white font-semibold py-2 px-4 rounded-md hover:bg-blue-700 transition mt-4">返回重试</a>
        {% if me_url %}
        {# 错误状态也用 POST 表单，确保点击直接重试 #}
        <form method="POST" action="/me/{{ me_url.split('/')[-1] }}/start-parent-subscribe" class="block mt-2">
            <button type="submit" class="w-full bg-gradient-to-r from-amber-500 to-rose-500 text-white font-bold py-2.5 px-4 rounded-md hover:from-amber-600 hover:to-rose-600 transition">
                📨 重试家长订阅版
            </button>
        </form>
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
    luogu_uid = str(request.args.get("luogu_uid", "") or task.get("luogu_uid", "") or "")
    me_url = f"/me/{luogu_uid}" if luogu_uid and luogu_uid.isdigit() else ""
    return render_template_string(
        STATUS_HTML,
        status=task.get("status", "unknown"),
        message=task.get("message", ""),
        stage=str(task.get("stage", "") or ""),
        task_type=str(task.get("task_type", "") or ""),
        source_code_success=int(task.get("source_code_success", 0) or 0),
        source_code_total=int(task.get("source_code_total", 0) or 0),
        tag_fetch_success=int(task.get("tag_fetch_success", 0) or 0),
        tag_fetch_total=int(task.get("tag_fetch_total", 0) or 0),
        ai_progress=int(task.get("ai_progress", 0) or 0),
        ai_elapsed_seconds=int(task.get("ai_elapsed_seconds", 0) or 0),
        html=task.get("html", ""),
        pdf=_download_report_url(pdf_url),
        md=task.get("md", ""),
        ps_html=task.get("ps_html", ""),
        ps_md=task.get("ps_md", ""),
        retry_url=url_for("retry_task", task_id=task_id),
        me_url=me_url,
        luogu_uid=luogu_uid,
    )


@app.route("/retry/<task_id>")
def retry_task(task_id):
    task = get_task(task_id) or {}
    snapshot = load_retry_form_snapshot(task)
    # 即使 snapshot 为空（cookies 等未缓存），也至少把学生基础字段回填并提示
    if not snapshot:
        return redirect("/")
    if can_resume_from_ai_stage(task):
        snapshot["resume_task_id"] = task_id
    # 兜底：若没有完整 retry_form_json（老 task 缺 cookies 缓存），flash 提示
    if not str(task.get("retry_form_json", "") or "").strip():
        flash("已自动回填「姓名 / 学校 / 年级」，Cookies / API Key 仍需补全后再生成。", "warning")
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
    # 关键修复：retry/重建路径之前直接复用磁盘上的 report.md，
    # 但 report.md 内的"知识点覆盖统计表"和"知识树"段是上一次跑出来的旧版本，
    # 即便 luogu_evaluator.py 改了也会一直保留。
    # 先抹掉已注入的可信块（避免被 strip 误吞），再让 normalize_report_markdown
    # 自动用最新代码覆盖旧表格/知识树，并把更新后的 markdown 写回 report.md。
    from luogu_evaluator import remove_injected_trusted_block, normalize_report_markdown
    report_md = normalize_report_markdown(remove_injected_trusted_block(report_md), export_data)
    md_path.write_text(report_md, encoding="utf-8")
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
    <div class="bg-white rounded-2xl card-shadow p-6 text-center relative">
        <!-- v3.6 分享图标按钮（fixed 浮在右上角，点开模态框展示海报） -->
        <button type="button" onclick="document.getElementById('shareModal').classList.remove('hidden')"
                class="absolute top-3 right-3 w-12 h-12 rounded-full bg-gradient-to-br from-emerald-500 to-teal-500 text-white text-xl shadow-lg hover:shadow-xl hover:scale-105 transition flex items-center justify-center"
                title="一键分享位置图（朋友圈/家长群）">
            📤
        </button>
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

    <!-- v3.6 分享海报模态框（点击右上角 📤 触发） -->
    <div id="shareModal" class="hidden fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4" onclick="if(event.target===this) this.classList.add('hidden')">
        <div class="bg-white rounded-2xl shadow-2xl max-w-2xl w-full max-h-[92vh] overflow-y-auto">
            <div class="sticky top-0 bg-white border-b border-gray-200 px-5 py-3 flex items-center justify-between z-10">
                <h3 class="text-base font-bold text-gray-800">🌱 9 月我家孩子位置 · 一键分享图</h3>
                <button type="button" onclick="document.getElementById('shareModal').classList.add('hidden')"
                        class="w-8 h-8 rounded-full hover:bg-gray-100 text-gray-500 text-lg flex items-center justify-center">✕</button>
            </div>
            <div class="p-5">
                <p class="text-sm text-gray-600 mb-3">
                    📌 把这张图发到家长群 / 朋友圈，分享孩子 GESP 段位 + 9 月免初赛倒计时 + 关键赛事路线。
                </p>
                <div class="flex justify-center bg-gray-50 border border-gray-200 rounded-lg p-2 mb-3">
                    <img id="shareCardImg" src="/me/{{ luogu_uid }}/share-card.png" alt="位置图海报"
                         class="max-w-full h-auto rounded shadow"
                         onerror="this.alt='海报生成失败 · 请刷新重试'; this.style.display='none';" />
                </div>
                <div class="flex flex-wrap items-center gap-2 justify-center">
                    <a href="/me/{{ luogu_uid }}/share-card.png" download="我家孩子位置图_{{ student.real_name or luogu_uid }}.png"
                       class="inline-flex items-center gap-1.5 px-4 py-2 bg-emerald-600 text-white text-sm font-bold rounded-lg hover:bg-emerald-700">
                        💾 保存图片
                    </a>
                    <a href="/me/{{ luogu_uid }}/share-card.png" target="_blank"
                       class="inline-flex items-center gap-1.5 px-4 py-2 bg-white border border-emerald-600 text-emerald-700 text-sm font-bold rounded-lg hover:bg-emerald-50">
                        🔗 在新窗口打开
                    </a>
                    <span class="text-xs text-gray-400 ml-2">PNG · 约 1 MB</span>
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


# 家长订阅版模板（5 维度深度分析 · 付费版）
PARENT_SUBSCRIBE_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>📨 家长订阅版 · {{ student.real_name or luogu_uid }}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body{background:linear-gradient(135deg,#fef3c7 0%,#fce7f3 50%,#e0e7ff 100%);min-height:100vh;font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;}
        .card-shadow{box-shadow:0 10px 25px rgba(0,0,0,.06);}
        .gradient-text{background:linear-gradient(90deg,#f59e0b,#ec4899);-webkit-background-clip:text;background-clip:text;color:transparent;}
        .countdown-pill{display:inline-block;padding:2px 8px;border-radius:9999px;font-size:11px;font-weight:600;}
        .copy-btn{cursor:pointer;transition:all .2s;}
        .copy-btn:hover{background:#f3f4f6;}
    </style>
</head>
<body class="p-4">
<div class="max-w-4xl mx-auto py-6 space-y-4">

    <!-- 头部：付费版品牌 + 学员信息 -->
    <div class="bg-white rounded-2xl card-shadow p-6">
        <div class="flex items-start justify-between flex-wrap gap-3">
            <div>
                <div class="flex items-center gap-2">
                    <span class="text-xs px-2 py-0.5 bg-amber-100 text-amber-700 rounded-full">📨 家长订阅版 · ¥30/月</span>
                    <span class="text-xs px-2 py-0.5 bg-rose-100 text-rose-700 rounded-full">v3.5.2 · 5 维度深度</span>
                </div>
                <h1 class="text-2xl font-extrabold text-gray-800 mt-2">{{ student.real_name or '选手' }} 的 OI 决策报告</h1>
                <p class="text-xs text-gray-500 mt-1">
                    {{ student.city or '未填城市' }}{% if student.province %} · {{ student.province }}{% endif %}
                    · {{ student.grade_label or student.grade or '—' }}
                    · UID {{ luogu_uid }}
                </p>
            </div>
            <div class="text-right">
                <div class="text-xs text-gray-400">当前段位</div>
                <div class="text-3xl font-extrabold gradient-text">
                    {% if gesp_level >= 8 %}🏆 G8{% elif gesp_level >= 7 %}🏆 G7{% elif gesp_level >= 4 %}🏅 G{{ gesp_level }}{% elif gesp_level >= 1 %}⭐ G{{ gesp_level }}{% else %}🌱 G0{% endif %}
                </div>
                <div class="text-xs text-gray-500 mt-1">最近分 {{ gesp_score or '—' }}</div>
            </div>
        </div>
    </div>

    <!-- 维度 1 · OI 生涯倒推（最核心） -->
    <div class="bg-white rounded-2xl card-shadow p-6">
        <h2 class="text-lg font-bold text-gray-800">🧭 维度 1 · OI 生涯路径倒推</h2>
        <p class="text-xs text-gray-500 mt-1">基于当前 GESP {{ gesp_level or 0 }} 级、最近分 {{ gesp_score or '—' }} 的 AI 估算 <span class="text-amber-600">（仅供参考）</span></p>

        <div class="mt-4 bg-gradient-to-r from-amber-50 to-rose-50 rounded-lg p-4">
            <div class="text-sm text-gray-600">推荐目标路径</div>
            <div class="text-xl font-bold text-gray-800 mt-1">{{ target }}</div>
        </div>

        <div class="mt-4 grid grid-cols-1 md:grid-cols-3 gap-3">
            <div class="border-2 border-emerald-200 bg-emerald-50 rounded-lg p-3">
                <div class="text-xs text-emerald-700 font-semibold">🐢 保守路线（稳扎稳打）</div>
                <div class="text-sm text-gray-800 mt-2 font-medium">{{ timeline.conservative }}</div>
            </div>
            <div class="border-2 border-rose-200 bg-rose-50 rounded-lg p-3">
                <div class="text-xs text-rose-700 font-semibold">🚀 激进路线（全力冲刺）</div>
                <div class="text-sm text-gray-800 mt-2 font-medium">{{ timeline.aggressive }}</div>
            </div>
            <div class="border-2 border-gray-200 bg-gray-50 rounded-lg p-3">
                <div class="text-xs text-gray-700 font-semibold">🛡️ 保底路线（不强求）</div>
                <div class="text-sm text-gray-800 mt-2 font-medium">{{ timeline.fallback }}</div>
            </div>
        </div>

        <p class="text-xs text-gray-400 mt-3">⚠️ 决策不假设一定要走 OI，3 档时间线仅供参考；具体路径请与教练面谈后确定。</p>
    </div>

    <!-- 维度 2 · 政策时间线匹配 -->
    <div class="bg-white rounded-2xl card-shadow p-6">
        <h2 class="text-lg font-bold text-gray-800">📅 维度 2 · 政策与赛事时间线</h2>
        <p class="text-xs text-gray-500 mt-1">距您家孩子最近的 8 个升学政策 / 赛事窗口</p>

        {% if policy_events %}
        <div class="mt-4 space-y-2">
            {% for ev in policy_events %}
            <div class="flex items-center justify-between border border-gray-200 rounded-lg p-3 hover:bg-gray-50">
                <div class="flex-1">
                    <div class="flex items-center gap-2">
                        <span class="text-sm font-bold text-gray-800">{{ ev.name }}</span>
                        <span class="countdown-pill {% if ev.days_left < 30 %}bg-red-100 text-red-700{% elif ev.days_left < 90 %}bg-amber-100 text-amber-700{% else %}bg-emerald-100 text-emerald-700{% endif %}">
                            {% if ev.days_left < 0 %}已过 {{ -ev.days_left }} 天{% elif ev.days_left == 0 %}今天{% else %}还有 {{ ev.days_left }} 天{% endif %}
                        </span>
                    </div>
                    <div class="text-xs text-gray-500 mt-1">{{ ev.date }} · {{ ev.category }}</div>
                    {% if ev.summary %}<div class="text-xs text-gray-600 mt-1">{{ ev.summary }}</div>{% endif %}
                </div>
            </div>
            {% endfor %}
        </div>
        {% else %}
        <p class="text-sm text-gray-400 mt-4">暂无可显示的政策事件</p>
        {% endif %}

        <p class="text-xs text-gray-400 mt-3">数据来源：competitions.json（最后更新请查看 admin 公告）</p>
    </div>

    <!-- 维度 3 · GESP 跳级 + 免初赛 -->
    <div class="bg-white rounded-2xl card-shadow p-6">
        <h2 class="text-lg font-bold text-gray-800">🎯 维度 3 · GESP 跳级 + 免初赛决策</h2>
        <p class="text-xs text-gray-500 mt-1">v3.5 引擎 A：4 条规则可视化</p>

        <div class="mt-4 grid grid-cols-2 md:grid-cols-4 gap-3">
            <!-- 规则 1：通过当前级别 -->
            <div class="border border-gray-200 rounded-lg p-3 text-center">
                <div class="text-2xl">{% if gesp_level >= 1 %}✅{% else %}⬜{% endif %}</div>
                <div class="text-xs text-gray-600 mt-1">规则 1：GESP {{ next_level }} 级 60+ 通过</div>
            </div>
            <!-- 规则 2：跳级 -->
            <div class="border border-gray-200 rounded-lg p-3 text-center">
                <div class="text-2xl">{% if gesp_level >= 1 and gesp_score >= 90 %}⭐{% else %}⬜{% endif %}</div>
                <div class="text-xs text-gray-600 mt-1">规则 2：90+ 跳 2 级</div>
            </div>
            <!-- 规则 4a：免 CSP-J -->
            <div class="border {% if can_exempt_cspj %}border-amber-300 bg-amber-50{% else %}border-gray-200{% endif %} rounded-lg p-3 text-center">
                <div class="text-2xl">{% if can_exempt_cspj %}🏆{% else %}⬜{% endif %}</div>
                <div class="text-xs text-gray-600 mt-1">规则 4a：G7 80+ 免 CSP-J 初赛</div>
            </div>
            <!-- 规则 4c：免 CSP-S -->
            <div class="border {% if can_exempt_csps %}border-rose-300 bg-rose-50{% else %}border-gray-200{% endif %} rounded-lg p-3 text-center">
                <div class="text-2xl">{% if can_exempt_csps %}🏆{% else %}⬜{% endif %}</div>
                <div class="text-xs text-gray-600 mt-1">规则 4c：G8 80+ 免 CSP-S 初赛</div>
            </div>
        </div>

        <div class="mt-4 bg-amber-50 border border-amber-200 rounded-lg p-3">
            <p class="text-sm text-amber-800">
                <strong>距 {{ next_level }} 级还差 {{ gesp_gap }} 分</strong>（AI 估算，仅供参考）
                {% if gesp_level >= 1 %}· 当前级别 {{ gesp_level }}，最近分 {{ gesp_score }}{% endif %}
            </p>
        </div>

        <div class="mt-3 grid grid-cols-1 md:grid-cols-3 gap-2 text-xs">
            <div class="bg-gray-50 rounded p-2">
                <strong>✅ 选项 A · 直接参加 {{ next_level }} 级：</strong>稳，最保守
            </div>
            <div class="bg-gray-50 rounded p-2">
                <strong>⭐ 选项 B · 跳级（{{ next_level + 1 if next_level < 8 else 8 }} 级）：</strong>需 90+，有失败风险
            </div>
            <div class="bg-gray-50 rounded p-2">
                <strong>🛡️ 选项 C · 暂缓：</strong>等基础更扎实再考
            </div>
        </div>
    </div>

    <!-- 维度 4 · 学员当前状态诊断 -->
    <div class="bg-white rounded-2xl card-shadow p-6">
        <h2 class="text-lg font-bold text-gray-800">🔍 维度 4 · 学员当前状态诊断（家长友好版）</h2>
        <p class="text-xs text-gray-500 mt-1">术语翻译：难度/算法标签解释为"学习水平分布"</p>

        <div class="mt-4 grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
                <h3 class="text-sm font-bold text-gray-700 mb-2">📊 难度分布（最近一次测评）</h3>
                {% if diff_dist %}
                <div class="space-y-1">
                    {% for lvl, cnt in diff_dist.items() %}
                    <div class="flex items-center gap-2 text-sm">
                        <span class="w-16 text-gray-600">{{ lvl }}</span>
                        <div class="flex-1 bg-gray-200 rounded h-3 overflow-hidden">
                            <div class="bg-blue-500 h-3" style="width: {{ (cnt / (diff_dist.values()|list|max) * 100)|int if cnt else 0 }}%"></div>
                        </div>
                        <span class="w-10 text-right text-gray-700 font-medium">{{ cnt }}</span>
                    </div>
                    {% endfor %}
                </div>
                {% else %}
                <p class="text-sm text-gray-400">暂未抓取到难度分布</p>
                {% endif %}
            </div>
            <div>
                <h3 class="text-sm font-bold text-gray-700 mb-2">🕐 最近 GESP 真考</h3>
                {% if last_exam %}
                <div class="text-sm text-gray-700 space-y-1">
                    <div>级别：<strong>{{ last_exam.level or '—' }}</strong></div>
                    <div>分数：<strong>{{ last_exam.score or '—' }}</strong></div>
                    <div>时间：<strong>{{ last_exam.exam_date or last_exam.award_year or '—' }}</strong></div>
                </div>
                {% else %}
                <p class="text-sm text-gray-400">暂无 GESP 真考记录</p>
                <p class="text-xs text-gray-400 mt-1">💡 在 <a href="/me/{{ luogu_uid }}" class="text-emerald-600 hover:underline">学员中心</a> 可自助录入</p>
                {% endif %}
            </div>
        </div>

        <p class="text-xs text-gray-400 mt-3">完整数据看板请见 <a href="/report/student/{{ luogu_uid }}" class="text-emerald-600 hover:underline">学员版报告</a></p>
    </div>

    <!-- 维度 5 · 教练沟通清单 -->
    <div class="bg-white rounded-2xl card-shadow p-6">
        <h2 class="text-lg font-bold text-gray-800">💬 维度 5 · 教练沟通清单</h2>
        <p class="text-xs text-gray-500 mt-1">下次面谈直接对照问，下方按钮一键复制</p>

        <ol class="mt-4 space-y-2">
            {% for q in questions %}
            <li class="flex items-start gap-2 border border-gray-200 rounded-lg p-3 hover:bg-gray-50">
                <span class="text-xs font-bold text-amber-600 mt-0.5">{{ loop.index }}.</span>
                <span class="text-sm text-gray-800 flex-1">{{ q }}</span>
                <button class="copy-btn text-xs px-2 py-1 border border-gray-300 rounded text-gray-600"
                        onclick="navigator.clipboard.writeText(this.previousElementSibling.textContent);this.textContent='✓ 已复制';setTimeout(()=>this.textContent='复制', 1500);">
                    复制
                </button>
            </li>
            {% endfor %}
        </ol>

        <div class="mt-4 flex gap-2">
            <button onclick="var qs=Array.from(document.querySelectorAll('ol li span:nth-child(2)')).map(s=>s.textContent).join('\\n\\n');navigator.clipboard.writeText(qs);this.textContent='✓ 全部已复制';"
                    class="flex-1 bg-amber-500 hover:bg-amber-600 text-white font-semibold py-2 rounded-md transition">
                📋 复制全部 7 个问题
            </button>
            <a href="mailto:?subject={{ student.real_name or '学员' }} 的 OI 决策沟通清单&body={{ (questions|join('%0D%0A%0D%0A'))|urlencode }}"
               class="flex-1 bg-gray-700 hover:bg-gray-800 text-white font-semibold py-2 rounded-md text-center transition">
                📧 发到教练邮箱
            </a>
        </div>
    </div>

    <!-- 触发 AI 真生成表单（仅当已生成基础报告、且还没有家长订阅版时显示） -->
    {% if has_report %}
    <div class="bg-gradient-to-r from-amber-50 to-rose-50 rounded-2xl card-shadow p-6 border-2 border-amber-200">
        <h2 class="text-lg font-bold text-gray-800">🚀 触发生成 AI 家长订阅版</h2>
        <p class="text-xs text-gray-600 mt-1">基于同账号的报告 {{ report_dir_name }} 让 AI 重新写一份"家长视角"的深度分析（约 1-2 分钟）</p>
        {% if error_msg %}
        <div class="mt-3 bg-rose-100 border border-rose-300 text-rose-800 text-sm p-3 rounded">❌ {{ error_msg }}</div>
        {% endif %}
        <form method="POST" action="/me/{{ luogu_uid }}/start-parent-subscribe" class="mt-4 space-y-3">
            <div>
                <label class="text-xs text-gray-600">OpenAI API Key（如服务端已设环境变量可留空）</label>
                <input type="password" name="api_key" placeholder="sk-..." class="w-full mt-1 px-3 py-2 border border-gray-300 rounded text-sm focus:border-amber-500 focus:outline-none">
            </div>
            <div class="grid grid-cols-2 gap-3">
                <div>
                    <label class="text-xs text-gray-600">模型名（默认 gpt-4o-mini）</label>
                    <input type="text" name="model_name" placeholder="gpt-4o-mini" class="w-full mt-1 px-3 py-2 border border-gray-300 rounded text-sm">
                </div>
                <div>
                    <label class="text-xs text-gray-600">Base URL（可留空）</label>
                    <input type="text" name="base_url" placeholder="https://api.openai.com/v1" class="w-full mt-1 px-3 py-2 border border-gray-300 rounded text-sm">
                </div>
            </div>
            <button type="submit" class="w-full bg-gradient-to-r from-amber-500 to-rose-500 hover:from-amber-600 hover:to-rose-600 text-white font-bold py-3 rounded-md transition">
                📨 开始 AI 真生成 · 约 1-2 分钟
            </button>
        </form>
    </div>
    {% else %}
    <div class="bg-rose-50 rounded-2xl card-shadow p-6 border-2 border-rose-200">
        <h2 class="text-lg font-bold text-rose-700">⚠️ 还没生成过基础报告</h2>
        <p class="text-sm text-rose-600 mt-2">家长订阅版需要在基础报告之上做 AI 二次生成。请先回到 <a href="/" class="underline">生成报告页</a> 跑一次。</p>
    </div>
    {% endif %}

    <!-- 底部 · Tab 切换 + 升级提示 -->
    <div class="bg-white rounded-2xl card-shadow p-4">
        <div class="flex gap-2">
            <a href="/report/student/{{ luogu_uid }}" class="flex-1 text-center py-2 rounded-lg bg-emerald-500 text-white font-bold text-sm">🎓 学员版</a>
            <a href="/report/parent/{{ luogu_uid }}" class="flex-1 text-center py-2 rounded-lg bg-amber-500 text-white font-bold text-sm">👨‍👩‍👧 家长版</a>
            <a href="/me/{{ luogu_uid }}/parent-subscribe" class="flex-1 text-center py-2 rounded-lg bg-gradient-to-r from-amber-500 to-rose-500 text-white font-bold text-sm">📨 订阅版（当前）</a>
        </div>
        <p class="text-center text-xs text-gray-400 mt-3">
            v3.5.2 · 家长订阅版 · AI 估算水印 · 数据更新于 {{ student.updated_at or '—' }}
        </p>
        <p class="text-center text-xs text-gray-400 mt-2">
            💎 订阅状态：<span class="font-bold {% if has_parent_sub %}text-emerald-600{% else %}text-rose-600{% endif %}">
                {% if has_parent_sub %}已订阅（有效期内）{% else %}未订阅 · <a href="/redeem" class="underline">激活订阅</a>{% endif %}
            </span>
        </p>
    </div>
</div>
</body>
</html>
"""


# 家长订阅版结果模板（AI 已生成 → 渲染外层壳 + AI 主体）
_PARENT_SUBSCRIBE_SHELL_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>📨 {{ student_name }} 的家长订阅版</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body{background:linear-gradient(135deg,#fef3c7 0%,#fce7f3 50%,#e0e7ff 100%);min-height:100vh;font-family:ui-sans-serif,system-ui,sans-serif;}
        .container{max-width:920px;margin:0 auto;padding:24px 16px;}
        .card{background:#fff;border-radius:18px;box-shadow:0 10px 25px rgba(0,0,0,.06);padding:32px;margin-bottom:16px;}
        .ai-body h1{font-size:1.5rem;font-weight:800;margin:24px 0 12px;color:#1f2937;}
        .ai-body h2{font-size:1.2rem;font-weight:700;margin:24px 0 10px;color:#1f2937;border-left:4px solid #f59e0b;padding-left:10px;}
        .ai-body h3{font-size:1.05rem;font-weight:700;margin:16px 0 8px;color:#374151;}
        .ai-body p{margin:8px 0;line-height:1.7;color:#374151;}
        .ai-body ul,.ai-body ol{margin:8px 0 12px 24px;line-height:1.7;}
        .ai-body li{margin:4px 0;}
        .ai-body table{width:100%;border-collapse:collapse;margin:12px 0;}
        .ai-body th,.ai-body td{border:1px solid #e5e7eb;padding:6px 10px;text-align:left;}
        .ai-body th{background:#f9fafb;font-weight:600;}
        .ai-body code{background:#f3f4f6;padding:1px 6px;border-radius:4px;font-size:0.9em;}
        .ai-body blockquote{border-left:4px solid #cbd5e1;padding:6px 12px;color:#6b7280;background:#f8fafc;margin:12px 0;border-radius:4px;}
        .ai-body hr{border:0;border-top:1px dashed #e5e7eb;margin:20px 0;}
        .ai-body strong{color:#b45309;}
    </style>
</head>
<body>
<div class="container">
    <div class="card">
        <div class="flex items-center justify-between flex-wrap gap-3">
            <div>
                <span class="inline-block text-xs px-2 py-0.5 bg-amber-100 text-amber-700 rounded-full">📨 家长订阅版 · AI 真生成</span>
                <h1 class="text-2xl font-extrabold text-gray-800 mt-2">{{ student_name }} 的家长订阅版深度分析</h1>
                <p class="text-xs text-gray-500 mt-1">UID {{ luogu_uid }} · 生成于 {{ generated_at }} · 报告目录 {{ report_dir_name }}</p>
            </div>
            <div class="flex gap-2">
                <a href="/me/{{ luogu_uid }}" class="text-xs px-3 py-1.5 bg-emerald-100 text-emerald-700 rounded-md hover:bg-emerald-200">🎓 学员中心</a>
                <a href="/report/student/{{ luogu_uid }}" target="_blank" class="text-xs px-3 py-1.5 bg-blue-100 text-blue-700 rounded-md hover:bg-blue-200">📊 学员版报告</a>
                <a href="/report/parent/{{ luogu_uid }}" target="_blank" class="text-xs px-3 py-1.5 bg-purple-100 text-purple-700 rounded-md hover:bg-purple-200">👨‍👩‍👧 家长版报告</a>
            </div>
        </div>
        <div class="mt-3 text-xs text-gray-500 bg-amber-50 border border-amber-200 rounded p-2">
            ⚠️ 本报告由 AI 二次生成，基于同账号的洛谷 AI 报告（report.md）作为上下文。
            内容仅供家长参考，**不构成正式教育建议**。重要决策请与教练面谈。
        </div>
    </div>
    <div class="card">
        <div class="ai-body">
            {{ ai_body|safe }}
        </div>
    </div>
    <div class="text-center text-xs text-gray-400 py-4">
        v3.5.2 · 家长订阅版 · AI 估算水印 · 报告目录 {{ report_dir_name }}
    </div>
</div>
</body>
</html>
"""


# 兼容旧引用名（run_parent_subscribe 用 _PARENT_SUBSCRIBE_SHELL_HTML，
# 但 PARENT_SUBSCRIBE_RESULT_HTML 是给 GET 路由用别名）
PARENT_SUBSCRIBE_RESULT_HTML = _PARENT_SUBSCRIBE_SHELL_HTML


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
            <a href="/admin/codes" class="block text-center py-2 rounded-lg bg-blue-50 text-blue-700 text-sm font-bold hover:bg-blue-100">兑换码生成</a>
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
    return render_template_string(
        GENERATE_FORM_HTML,
        form={},
        server_key_hint=_get_server_key_hint(),
        validation_result=request.args.get("validation_result"),
    )


@app.route("/validate-cookies-v352", methods=["POST"])
def validate_cookies_v352():
    """v3.5.2 表单的 Cookie 预校验：仅校验 + 原地重渲染，不进入主提交流程"""
    form = request.form.to_dict()
    return render_template_string(
        GENERATE_FORM_HTML,
        form=form,
        server_key_hint=_get_server_key_hint(),
        validation_result=validate_cookies(form),
    )


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

        <div class="bg-amber-50 border border-amber-200 rounded-lg p-3 text-xs text-amber-800 space-y-2">
            <p class="font-semibold text-sm">如何获取洛谷 Cookies：</p>
            <ol class="list-decimal list-inside space-y-1 text-amber-700">
                <li>打开 <code>https://www.luogu.com.cn</code> 并登录</li>
                <li>在洛谷页面 <kbd class="px-1 bg-amber-100 rounded">右键</kbd> → <kbd class="px-1 bg-amber-100 rounded">检查</kbd> → <kbd class="px-1 bg-amber-100 rounded">Application(应用)</kbd> → <kbd class="px-1 bg-amber-100 rounded">Storage(存储)</kbd> → <kbd class="px-1 bg-amber-100 rounded">Cookies</kbd> → <code>https://www.luogu.com.cn</code></li>
                <li>复制以下三个参数的 Name/Value 填入下方：</li>
            </ol>
            <details class="mt-1">
                <summary class="cursor-pointer select-none text-amber-800 hover:text-amber-900 font-medium">
                    📷 查看指引图（点击展开 / 折叠）
                </summary>
                <div class="mt-2 p-2 bg-white border border-amber-200 rounded-md">
                    <img src="{{ url_for('static', filename='cookie_guide.png') }}"
                         alt="如何获取洛谷 Cookies 指引图"
                         class="block w-full h-auto rounded-sm shadow-sm" />
                    <p class="mt-2 text-amber-700 leading-relaxed">
                        <span class="font-semibold">高亮的三行</span>就是需要复制的字段：
                        <code>__client_id</code> / <code>_uid</code> / <code>C3VK</code>。
                        点击右侧"复制"按钮可一键复制 Value。
                    </p>
                </div>
            </details>
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

            <!-- 先校验 Cookies（推荐）— 与旧表单一致 -->
            <div class="mt-3 bg-blue-50 border border-blue-200 rounded-lg p-3">
                <div class="flex items-start justify-between gap-3">
                    <div>
                        <p class="font-semibold text-blue-800">先校验 Cookies（推荐）</p>
                        <p class="text-xs text-blue-700 mt-1">填写完上面三个参数后点一次，立刻检查 me / practice / record/list 是否可用。</p>
                    </div>
                </div>
                <div class="mt-3">
                    <button id="v352ValidateBtn" type="submit"
                            formaction="/validate-cookies-v352"
                            formnovalidate
                            class="w-full bg-white text-blue-700 font-semibold py-2 px-4 rounded-md border border-blue-300 hover:bg-blue-50 transition disabled:opacity-50 disabled:cursor-not-allowed">
                        校验 Cookies
                    </button>
                </div>
                <p id="v352ValidateHint" class="text-xs text-blue-700 mt-2">
                    请先填写 __client_id、_uid、C3VK 后再校验。
                </p>
                {% if validation_result %}
                <div class="mt-2 rounded-md p-2 text-sm {% if validation_result.ok %}bg-green-50 border border-green-200 text-green-800{% else %}bg-red-50 border border-red-200 text-red-800{% endif %}">
                    <p class="font-semibold">{{ validation_result.title }}</p>
                    <p>{{ validation_result.message }}</p>
                </div>
                {% endif %}
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
        <details class="field-section" open>
            <summary class="cursor-pointer text-sm font-bold text-gray-600 hover:text-emerald-600">🎂 3. 选手信息（可选 · 用于选手竞赛生涯规划，不填也能生成报告）</summary>
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

            <!-- v3.6 整合：自录历史奖项入口（指向个人中心 #awards） -->
            <div class="mt-4 pt-4 border-t border-gray-200">
                <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
                    <!-- GESP 真考 · 入口卡片 -->
                    <a href="/me/{{ form.get('uid','') }}#awards" class="block bg-green-50 border border-green-200 rounded-lg p-3 hover:bg-green-100 transition">
                        <div class="flex items-center gap-2 mb-1">
                            <span class="text-base">🎯</span>
                            <h4 class="text-sm font-bold text-green-800">GESP 真考（CCF 1-8 级）</h4>
                        </div>
                        <p class="text-xs text-gray-600 leading-relaxed">录入等级、分数、年份、证书编号<br>自动计算 9 月免初赛 + 段位</p>
                        <p class="text-xs text-green-700 font-semibold mt-2">→ 录入 GESP 真考</p>
                    </a>
                    <!-- CSP/NOIP/NOI · 入口卡片 -->
                    <a href="/me/{{ form.get('uid','') }}#awards" class="block bg-blue-50 border border-blue-200 rounded-lg p-3 hover:bg-blue-100 transition">
                        <div class="flex items-center gap-2 mb-1">
                            <span class="text-base">🏅</span>
                            <h4 class="text-sm font-bold text-blue-800">CSP / NOIP / NOI 奖项</h4>
                        </div>
                        <p class="text-xs text-gray-600 leading-relaxed">录入比赛类型、奖项、年份、分数、省份<br>自动写入选手履历</p>
                        <p class="text-xs text-blue-700 font-semibold mt-2">→ 录入 CSP/NOIP/NOI 奖项</p>
                    </a>
                </div>
                <p class="text-[10px] text-gray-400 mt-2 text-center">
                    💡 提交报告后，可在「个人中心 → 📥 自录历史奖项」直接录入；已注册学员会立即看到完整表单
                </p>
            </div>
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

        <p class="text-center text-xs text-gray-400">生成后可在 <a href="/me" class="text-emerald-600 hover:underline">/me/&lt;你的 UID&gt;</a> 查看 3 版本报告（学员·家长·教练）</p>
    </form>

    <div class="text-center">
        <a href="/" class="text-xs text-gray-400 hover:text-emerald-600">← 返回首页</a>
    </div>
</div>

<script>
    (function () {
        function v(id) {
            var el = document.querySelector('input[name="' + id + '"]');
            return el ? (el.value || '').trim() : '';
        }
        var btn  = document.getElementById('v352ValidateBtn');
        var hint = document.getElementById('v352ValidateHint');
        function refresh() {
            var ok = !!v('client_id') && !!v('uid') && !!v('c3vk');
            if (btn)  btn.disabled  = !ok;
            if (hint) hint.textContent = ok
                ? '已填写三个参数，建议先点一次校验。'
                : '请先填写 __client_id、_uid、C3VK 后再校验。';
        }
        ['client_id', 'uid', 'c3vk'].forEach(function (name) {
            var el = document.querySelector('input[name="' + name + '"]');
            if (el) el.addEventListener('input', refresh);
        });
        refresh();
    })();
</script>
</body>
</html>
"""


@app.route("/select-mode", methods=["GET", "POST"])
def select_mode():
    """v3.5.2 老用户快速入口（已注册选手输 UID 直接看报告，不生成新报告）"""
    import re as _re
    import os as _os
    _uid_guide_exists = _os.path.exists(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "static", "uid_guide.png"))
    if request.method == "GET":
        return render_template_string(SELECT_MODE_HTML, error=None, form={}, static_exists_uid_guide=_uid_guide_exists)
    # POST 接收 luogu_uid → 校验 → 引导身份
    luogu_uid = (request.form.get("luogu_uid") or "").strip()
    if not _re.match(r"^\d{6,10}$", luogu_uid):
        return render_template_string(
            SELECT_MODE_HTML,
            error="请输入 6-10 位洛谷 UID",
            form={"luogu_uid": luogu_uid},
            static_exists_uid_guide=_uid_guide_exists,
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

                <!-- UID 获取图文指引（点击展开 / 折叠） -->
                <details class="mt-2">
                    <summary class="cursor-pointer select-none text-xs text-emerald-700 hover:text-emerald-800 font-medium">
                        📷 不知道怎么获取 UID？（点击展开指引）
                    </summary>
                    <div class="mt-2 p-3 bg-gray-50 border border-gray-200 rounded-md text-xs text-gray-600 space-y-2">
                        <p>
                            <span class="font-semibold text-gray-700">方法 1：</span>在洛谷
                            <a href="https://www.luogu.com.cn" target="_blank" class="text-emerald-600 hover:underline">luogu.com.cn</a>
                            登录后，<strong>点击右上角个人头像</strong>，浏览器地址栏会出现：
                        </p>
                        <div class="bg-white border border-gray-200 rounded px-2 py-1.5 font-mono text-[11px] text-gray-700">
                            🔗 https://www.luogu.com.cn/user/<span class="text-red-500 font-bold bg-yellow-100 px-1 rounded">1054015</span>
                        </div>
                        <p>其中 <code class="bg-yellow-100 px-1 rounded text-red-600 font-bold">红色高亮的数字</code> 就是你的洛谷 UID（6-10 位）。</p>
                        <p>
                            <span class="font-semibold text-gray-700">方法 2：</span>在洛谷任一题解/记录页，
                            <strong>点击作者名</strong>跳转到个人主页，地址栏 <code>/user/</code> 后面就是 UID。
                        </p>
                        <p>
                            <span class="font-semibold text-gray-700">方法 3：</span>找已经报过名的同学问 UID（每位用户的 UID 唯一不变）。
                        </p>
                        {% if static_exists_uid_guide %}
                        <img src="{{ url_for('static', filename='uid_guide.png') }}"
                             alt="UID 获取指引图"
                             class="block w-full h-auto rounded-sm border border-gray-200 mt-2" />
                        {% endif %}
                    </div>
                </details>
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
        notice=(
            "🛡️ 9 月传播期：兑换码激活仅供教练/客服手工使用；"
            "C 端家长请向教练索取" if _HIDE_COMMERCE else None
        ),
        commerce_hidden=_HIDE_COMMERCE,
    )


@app.route("/coach")
def coach_landing():
    """v3.5.2 教练版咨询入口（B2B · 联系客服购买）"""
    return render_template_string(COACH_LANDING_HTML)


# ============================================================
# v3.5.2 · 兑换码管理 /admin/codes（列表 + 批量生成 + 复制）
# ============================================================
# 功能：
#   GET  ?sku=parent_sub&status=unused  列表（按 sku / 状态过滤）
#   POST action=generate                批量生成（sku + count + duration）
#   POST action=delete                  单条删除（仅未使用）
# ============================================================
import secrets as _secrets_admin_codes  # noqa: E402 局部别名，避免污染其他文件

_SKU_PRESETS = [
    # (value, label, default_duration_days, hint)
    ("parent_sub",       "家长订阅",      30,  "¥30/月 · 解锁孩子 OI 家长视角 + 错题讲解"),
    ("popularize_camp",  "普及组冲刺营",  28,  "¥99/4 周 · GESP 7 级 80+/8 级 60+ → 免 CSP-J 初赛"),
    ("improve_camp",     "提高组冲刺营",  56,  "¥299/8 周 · GESP 8 级 80+ → 免 CSP-S 初赛"),
]
_SKU_DURATIONS = {v[0]: v[2] for v in _SKU_PRESETS}
_SKU_LABELS = {v[0]: v[1] for v in _SKU_PRESETS}


def _generate_activation_code(sku: str) -> str:
    """生成不重复的兑换码：<SKU 前缀>-<8 位 A-Z0-9>
    前缀取 sku 单词首字母（parent_sub → PS · popularize_camp → PJC · improve_camp → IJC），
    与历史生成码兼容（PJC-*/IJC-* 已存在 14 个）。
    """
    prefix_map = {"parent_sub": "PS", "popularize_camp": "PJC", "improve_camp": "IJC"}
    prefix = prefix_map.get(sku, "AC")
    return f"{prefix}-" + "".join(
        _secrets_admin_codes.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8)
    )


@app.route("/admin/codes", methods=["GET", "POST"])
def admin_codes():
    """v3.5.2 兑换码生成 / 列表（教练 / 客服后台）"""
    auth_redirect = require_admin_auth()
    if auth_redirect is not None:
        return auth_redirect

    flash = None
    flash_type = "success"

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        if action == "generate":
            sku = (request.form.get("sku") or "").strip()
            if sku not in _SKU_DURATIONS:
                flash, flash_type = f"未知 SKU：{sku}", "error"
            else:
                try:
                    count = int(request.form.get("count") or "1")
                except Exception:
                    count = 0
                if count < 1 or count > 200:
                    flash, flash_type = "生成数量需在 1-200 之间", "error"
                else:
                    try:
                        duration_days = int(
                            request.form.get("duration_days") or _SKU_DURATIONS[sku]
                        )
                    except Exception:
                        duration_days = _SKU_DURATIONS[sku]
                    if duration_days < 1 or duration_days > 3650:
                        flash, flash_type = "有效期需在 1-3650 天之间", "error"
                    else:
                        try:
                            from task_store import _get_conn
                            conn = _get_conn()
                            try:
                                created = []
                                # 简单去重：批量生成 + 查重补打（最多 5 轮重试）
                                for _ in range(count):
                                    inserted = False
                                    for _retry in range(5):
                                        code = _generate_activation_code(sku)
                                        try:
                                            conn.execute(
                                                "INSERT INTO activation_codes "
                                                "(code, sku, duration_days, created_by) "
                                                "VALUES (?, ?, ?, ?)",
                                                (code, sku, duration_days, "admin"),
                                            )
                                            created.append(code)
                                            inserted = True
                                            break
                                        except sqlite3.IntegrityError:
                                            continue
                                    if not inserted:
                                        raise RuntimeError("兑换码去重失败 5 次，请重试")
                                conn.commit()
                            finally:
                                conn.close()
                            flash = (
                                f"已生成 {len(created)} 个 "
                                f"{_SKU_LABELS.get(sku, sku)} 码（{duration_days} 天有效期）"
                            )
                            flash_type = "success"
                            # 把刚生成的码塞进 session-like query 让页面高亮
                            return redirect(
                                url_for(
                                    "admin_codes",
                                    sku=sku,
                                    status="unused",
                                    notice=flash,
                                    notice_type=flash_type,
                                    highlight=",".join(created),
                                )
                            )
                        except Exception as e:
                            flash, flash_type = f"生成失败：{e}", "error"
        elif action == "delete":
            try:
                code_id = int(request.form.get("id") or "0")
            except Exception:
                code_id = 0
            if code_id <= 0:
                flash, flash_type = "无效的兑换码 ID", "error"
            else:
                try:
                    from task_store import _get_conn
                    conn = _get_conn()
                    try:
                        row = conn.execute(
                            "SELECT code, redeemed_at FROM activation_codes WHERE id = ?",
                            (code_id,),
                        ).fetchone()
                        if not row:
                            flash, flash_type = "兑换码不存在", "error"
                        elif row["redeemed_at"]:
                            flash, flash_type = f"兑换码 {row['code']} 已被使用，不能删除", "error"
                        else:
                            conn.execute(
                                "DELETE FROM activation_codes WHERE id = ?", (code_id,)
                            )
                            conn.commit()
                            flash, flash_type = (
                                f"已删除兑换码 {row['code']}",
                                "success",
                            )
                    finally:
                        conn.close()
                except Exception as e:
                    flash, flash_type = f"删除失败：{e}", "error"
        else:
            flash, flash_type = f"未知操作：{action}", "error"

        if flash and request.endpoint == "admin_codes":
            return redirect(
                url_for(
                    "admin_codes",
                    sku=request.args.get("sku", ""),
                    status=request.args.get("status", ""),
                    notice=flash,
                    notice_type=flash_type,
                )
            )

    # ----- GET 列表 -----
    filter_sku = (request.args.get("sku") or "").strip()
    filter_status = (request.args.get("status") or "").strip()  # unused | used | all
    notice = (request.args.get("notice") or "").strip()
    notice_type = (request.args.get("notice_type") or "success").strip()
    highlight_codes = set(
        (request.args.get("highlight") or "").split(",")
        if request.args.get("highlight")
        else []
    )

    # 拉表 + 关联 students
    from task_store import _get_conn
    conn = _get_conn()
    try:
        where = []
        params: list = []
        if filter_sku and filter_sku in _SKU_LABELS:
            where.append("ac.sku = ?")
            params.append(filter_sku)
        if filter_status == "unused":
            where.append("ac.redeemed_at IS NULL")
        elif filter_status == "used":
            where.append("ac.redeemed_at IS NOT NULL")
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"SELECT ac.*, s.real_name AS stu_name, s.luogu_uid AS stu_uid "
            f"FROM activation_codes ac "
            f"LEFT JOIN students s ON s.id = ac.student_id "
            f"{where_sql} "
            f"ORDER BY ac.id DESC LIMIT 200",
            params,
        ).fetchall()

        # 统计
        stats = {}
        for sku_key in _SKU_LABELS.keys():
            s_row = conn.execute(
                "SELECT "
                "  COUNT(*) AS total, "
                "  SUM(CASE WHEN redeemed_at IS NULL THEN 1 ELSE 0 END) AS unused, "
                "  SUM(CASE WHEN redeemed_at IS NOT NULL THEN 1 ELSE 0 END) AS used "
                "FROM activation_codes WHERE sku = ?",
                (sku_key,),
            ).fetchone()
            stats[sku_key] = {
                "total": s_row["total"] or 0,
                "unused": s_row["unused"] or 0,
                "used": s_row["used"] or 0,
            }
    finally:
        conn.close()

    return render_template_string(
        ADMIN_CODES_HTML,
        codes=[dict(r) for r in rows],
        stats=stats,
        sku_presets=_SKU_PRESETS,
        sku_labels=_SKU_LABELS,
        sku_durations=_SKU_DURATIONS,
        filter_sku=filter_sku,
        filter_status=filter_status or "all",
        notice=notice,
        notice_type=notice_type,
        highlight_codes=highlight_codes,
    )


ADMIN_CODES_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>兑换码管理 · 信竞 AI 报告</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;background:#f9fafb;}
        .card{background:#fff;border-radius:14px;box-shadow:0 1px 3px rgba(0,0,0,.06);padding:18px;}
        .badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;}
        .code-cell{font-family:"JetBrains Mono","SF Mono",Consolas,monospace;letter-spacing:.5px;}
        .highlight-row{background:#fef9c3 !important;}
        .highlight-cell{background:#fde68a;padding:1px 4px;border-radius:3px;}
    </style>
</head>
<body class="min-h-screen p-4">
<div class="max-w-6xl mx-auto space-y-4">
    <div class="flex items-center justify-between">
        <div>
            <h1 class="text-2xl font-extrabold text-gray-800">🎟️ 兑换码管理</h1>
            <p class="text-xs text-gray-500 mt-1">v3.5.2 · 教练/客服后台 · 用于生成家长订阅 / 冲刺营激活码</p>
        </div>
        <div class="flex gap-2">
            <a href="/admin" class="text-xs px-3 py-1.5 bg-gray-100 text-gray-700 rounded-md hover:bg-gray-200">← 返回管理后台</a>
            <a href="/redeem" class="text-xs px-3 py-1.5 bg-blue-100 text-blue-700 rounded-md hover:bg-blue-200">📥 兑换入口（家长用）</a>
        </div>
    </div>

    {% if notice %}
    <div class="card border {% if notice_type == 'error' %}border-red-300 bg-red-50{% else %}border-emerald-300 bg-emerald-50{% endif %}">
        <p class="text-sm {% if notice_type == 'error' %}text-red-700{% else %}text-emerald-700{% endif %}">{{ notice }}</p>
    </div>
    {% endif %}

    <!-- 统计卡片 -->
    <div class="grid grid-cols-3 gap-3">
        {% for sku_key, sku_label in sku_labels.items() %}
        {% set st = stats[sku_key] %}
        <div class="card">
            <p class="text-xs text-gray-500">{{ sku_label }}</p>
            <p class="text-2xl font-extrabold text-gray-800 mt-1">{{ st.total }}</p>
            <p class="text-[11px] text-gray-500 mt-1">未用 <span class="font-bold text-emerald-600">{{ st.unused }}</span> · 已用 <span class="font-bold text-gray-700">{{ st.used }}</span></p>
        </div>
        {% endfor %}
    </div>

    <!-- 生成表单 -->
    <div class="card">
        <h2 class="text-base font-bold text-gray-800 mb-3">➕ 批量生成</h2>
        <form method="post" class="grid grid-cols-1 md:grid-cols-4 gap-3 items-end">
            <input type="hidden" name="action" value="generate">
            <div>
                <label class="block text-xs text-gray-600 mb-1">SKU 类型</label>
                <select name="sku" class="w-full text-sm border border-gray-300 rounded-md px-2 py-1.5" required>
                    {% for value, label, default_dur, hint in sku_presets %}
                    <option value="{{ value }}" {% if filter_sku == value %}selected{% endif %}>
                        {{ label }}（{{ hint }}）
                    </option>
                    {% endfor %}
                </select>
            </div>
            <div>
                <label class="block text-xs text-gray-600 mb-1">生成数量（1-200）</label>
                <input type="number" name="count" min="1" max="200" value="5"
                       class="w-full text-sm border border-gray-300 rounded-md px-2 py-1.5" required>
            </div>
            <div>
                <label class="block text-xs text-gray-600 mb-1">有效期（天，留空用默认值）</label>
                <input type="number" name="duration_days" min="1" max="3650" placeholder="默认"
                       class="w-full text-sm border border-gray-300 rounded-md px-2 py-1.5">
            </div>
            <div>
                <button type="submit" class="w-full text-sm font-bold bg-emerald-500 text-white py-2 rounded-md hover:bg-emerald-600">
                    🎲 一键生成
                </button>
            </div>
        </form>
    </div>

    <!-- 过滤 -->
    <div class="card flex flex-wrap items-center gap-2">
        <span class="text-xs text-gray-500">筛选：</span>
        <a href="/admin/codes" class="text-xs px-2 py-1 rounded {% if not filter_sku %}bg-gray-800 text-white{% else %}bg-gray-100 text-gray-700 hover:bg-gray-200{% endif %}">全部</a>
        {% for value, label, _d, _h in sku_presets %}
        <a href="/admin/codes?sku={{ value }}{% if filter_status %}&status={{ filter_status }}{% endif %}"
           class="text-xs px-2 py-1 rounded {% if filter_sku == value %}bg-blue-500 text-white{% else %}bg-gray-100 text-gray-700 hover:bg-gray-200{% endif %}">{{ label }}</a>
        {% endfor %}
        <span class="text-gray-300">|</span>
        <a href="/admin/codes{% if filter_sku %}?sku={{ filter_sku }}{% endif %}"
           class="text-xs px-2 py-1 rounded {% if filter_status == 'all' %}bg-gray-800 text-white{% else %}bg-gray-100 text-gray-700 hover:bg-gray-200{% endif %}">全部状态</a>
        <a href="/admin/codes{% if filter_sku %}?sku={{ filter_sku }}{% endif %}&status=unused"
           class="text-xs px-2 py-1 rounded {% if filter_status == 'unused' %}bg-emerald-500 text-white{% else %}bg-gray-100 text-gray-700 hover:bg-gray-200{% endif %}">未使用</a>
        <a href="/admin/codes{% if filter_sku %}?sku={{ filter_sku }}{% endif %}&status=used"
           class="text-xs px-2 py-1 rounded {% if filter_status == 'used' %}bg-amber-500 text-white{% else %}bg-gray-100 text-gray-700 hover:bg-gray-200{% endif %}">已使用</a>
    </div>

    <!-- 码表 -->
    <div class="card overflow-x-auto">
        <table class="w-full text-sm">
            <thead class="text-xs text-gray-500 border-b">
                <tr>
                    <th class="text-left py-2">#</th>
                    <th class="text-left py-2">兑换码</th>
                    <th class="text-left py-2">SKU</th>
                    <th class="text-right py-2">天数</th>
                    <th class="text-left py-2">状态</th>
                    <th class="text-left py-2">绑定学员</th>
                    <th class="text-left py-2">生成 / 激活</th>
                    <th class="text-right py-2">操作</th>
                </tr>
            </thead>
            <tbody>
            {% for c in codes %}
                <tr class="border-b border-gray-100 hover:bg-gray-50 {% if c.code in highlight_codes %}highlight-row{% endif %}">
                    <td class="py-2 text-gray-400">{{ c.id }}</td>
                    <td class="py-2">
                        <span class="code-cell {% if c.code in highlight_codes %}highlight-cell{% endif %} text-gray-800 font-bold">{{ c.code }}</span>
                    </td>
                    <td class="py-2">
                        <span class="badge bg-blue-100 text-blue-700">{{ sku_labels.get(c.sku, c.sku) }}</span>
                    </td>
                    <td class="py-2 text-right text-gray-600">{{ c.duration_days or '-' }}</td>
                    <td class="py-2">
                        {% if c.redeemed_at %}
                            <span class="badge bg-amber-100 text-amber-700">✅ 已用</span>
                        {% else %}
                            <span class="badge bg-emerald-100 text-emerald-700">未用</span>
                        {% endif %}
                    </td>
                    <td class="py-2 text-gray-700">
                        {% if c.stu_uid %}
                            <a href="/me/{{ c.stu_uid }}" class="text-blue-600 hover:underline">{{ c.stu_name or c.stu_uid }}</a>
                            <span class="text-gray-400 text-xs">({{ c.stu_uid }})</span>
                        {% else %}—{% endif %}
                    </td>
                    <td class="py-2 text-[11px] text-gray-500">
                        <div>生成 {{ (c.created_at or '')[:16] }}</div>
                        {% if c.redeemed_at %}<div class="text-emerald-600">激活 {{ c.redeemed_at[:16] }}</div>{% endif %}
                    </td>
                    <td class="py-2 text-right">
                        {% if not c.redeemed_at %}
                        <form method="post" class="inline" onsubmit="return confirm('确定删除兑换码 {{ c.code }}？');">
                            <input type="hidden" name="action" value="delete">
                            <input type="hidden" name="id" value="{{ c.id }}">
                            <button type="submit" class="text-xs px-2 py-1 bg-red-50 text-red-600 rounded hover:bg-red-100">删除</button>
                        </form>
                        {% else %}
                        <span class="text-xs text-gray-400">—</span>
                        {% endif %}
                    </td>
                </tr>
            {% else %}
                <tr><td colspan="8" class="py-8 text-center text-gray-400 text-sm">暂无兑换码 · 在上方表单生成第一批</td></tr>
            {% endfor %}
            </tbody>
        </table>
    </div>

    <p class="text-center text-xs text-gray-400">v3.5.2 · 兑换码管理 · PS = 家长订阅 / PJC = 普及冲刺 / IJC = 提高冲刺</p>
</div>
</body>
</html>
"""


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

        {% if notice %}
        <div class="mb-4 px-4 py-3 bg-gray-100 border border-gray-300 text-gray-700 rounded-lg text-sm">
            {{ notice }}
        </div>
        {% endif %}

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


# ---- /me（无 UID）→ UID 输入中转页 ----
# 场景：表单底部 / 状态页 / 邮件常写"在 /me 查看报告"，
# 用户自然输入 /me 进来；如果不引导就 404，会让用户误以为系统坏了。
_ME_PICKER_HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>进入个人中心 · 洛谷 AI 教练</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://cdn.tailwindcss.com"></script>
<style>body{background:#F0FDF4;font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;}</style>
</head>
<body class="min-h-screen flex items-center justify-center p-4">
  <div class="bg-white rounded-2xl shadow-lg border border-emerald-100 p-6 w-full max-w-md space-y-4">
    <div class="text-center">
      <div class="text-4xl mb-1">👋</div>
      <h1 class="text-xl font-bold text-gray-800">进入个人中心</h1>
      <p class="text-xs text-gray-500 mt-1">输入你的洛谷 UID，查看 3 版本学习报告（学员·家长·教练）</p>
    </div>

    <form id="meForm" action="/me/0" method="get" class="space-y-3"
          onsubmit="event.preventDefault();
                    var u=document.getElementById('meUid').value.trim();
                    if(!/^\\d{6,10}$/.test(u)){alert('请输入 6-10 位洛谷 UID');return;}
                    window.location.href='/me/'+u;">
      <label class="block text-sm font-medium text-gray-700">洛谷 UID</label>
      <input id="meUid" type="text" inputmode="numeric" pattern="\\d{6,10}"
             placeholder="如：582694（6-10 位数字）"
             class="w-full border border-gray-300 rounded-md p-2 focus:ring-2 focus:ring-emerald-400 focus:border-emerald-500"
             autofocus required>
      <button type="submit"
              class="w-full bg-emerald-600 hover:bg-emerald-700 text-white font-semibold py-2 rounded-md transition">
        进入个人中心 →
      </button>
    </form>

    <div class="border-t border-gray-200 pt-3 text-xs text-gray-500 space-y-1">
      <p>💡 不知道自己的 UID？</p>
      <ul class="list-disc list-inside space-y-0.5 text-gray-600">
        <li>登录 <a href="https://www.luogu.com.cn" target="_blank" class="text-emerald-600 hover:underline">luogu.com.cn</a>，点右上角头像，URL 里的数字就是 UID</li>
        <li>还没生成报告？<a href="/" class="text-emerald-600 hover:underline">先去生成 →</a></li>
      </ul>
    </div>
  </div>
</body>
</html>
"""


@app.route("/me", methods=["GET"])
@app.route("/me/", methods=["GET"])
def me_picker():
    """无 UID 的 /me 入口 → UID 输入中转页（避免 404 误判系统故障）"""
    return _ME_PICKER_HTML


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
        commerce_hidden=_HIDE_COMMERCE,
    )


# ---- v3.5.2 传播期 · 位置图分享卡（PNG） ----

import re as _re_share


def _shorten_comp_name(name: str) -> str:
    """把 CCF 全名压缩成海报可读形式，但**保留关键前缀**

    设计目标：海报宽度 800 像素，每行 13 字符以内为佳。
    关键前缀优先级（用户传播价值）：
      · GESP（CCF 编程能力等级认证）
      · CSP-J / CSP-S（CCF 软件能力认证普及/提高）
      · NOIP（全国青少年信息学奥林匹克联赛）
      · NOI（全国青少年信息学奥林匹克竞赛）
      · WC / APIO / CTSC（次要）
    """
    if "GESP" in name:
        # 注意：1-4 与"级"之间可能有半角空格，所以用 \s*
        m = _re_share.search(r"（([\d\-]+)\s*级\s*([春秋夏冬]?考)）", name)
        if m:
            level, season = m.group(1), m.group(2) or ""
            return f"GESP {level} 级（{season}）" if season else f"GESP {level} 级"
        return "GESP 等级认证"
    if "CSP-J" in name and "第一轮" in name:
        return "CSP-J 初赛"
    if "CSP-S" in name and "第一轮" in name:
        return "CSP-S 初赛"
    if "CSP-J" in name and "第二轮" in name:
        return "CSP-J 复赛"
    if "CSP-S" in name and "第二轮" in name:
        return "CSP-S 复赛"
    if "NOIP" in name:
        return "NOIP 全国赛"
    if "NOI" in name and "WC" not in name:
        return "NOI 信息学奥赛"
    if "WC" in name:
        return "WC 冬令营"
    if "CTSC" in name:
        return "CTSC 国家队选拔"
    if "APIO" in name:
        return "APIO 亚洲赛"
    return name


def _extract_ai_summary(report_md: str) -> str:
    """v3.7 · 从 report.md 抽「AI 核心解读」首段（≤200 字）。

    锚点：### （一）AI 核心解读 / ### 1. AI 核心解读 / ### AI 核心解读
    返回纯文本（去 markdown / 去多余空白），超 200 字截断。
    缺该节时返回 ""。
    """
    if not report_md:
        return ""
    import re as _re
    # 锚点变体
    m = _re.search(
        r"^#{2,4}\s*[（(]?[一二三四五六七八九十\d]+[)）]?\s*AI\s*核心解读.*?$",
        report_md, _re.M,
    )
    if not m:
        return ""
    body = report_md[m.end():]
    # 抓下一个二级或三级标题之前（仅 h2/h3 + 可选括号 + 中文/数字序号，避免 #### 子标题误终止）
    end_m = _re.search(r"^#{2,3}\s+[（(]?[一二三四五六七八九十\d]", body, _re.M)
    section = body[: end_m.start() if end_m else len(body)]
    # 去掉 markdown 标记 / 多余空白
    text = _re.sub(r"\*\*?(.+?)\*\*?", r"\1", section)
    text = _re.sub(r"`([^`]+)`", r"\1", text)
    text = _re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = _re.sub(r"\s+", " ", text).strip()
    if len(text) > 200:
        text = text[:200].rstrip() + "…"
    return text


def _extract_top_suggestions(report_md: str) -> list[str]:
    """v3.7 · 从 report.md 第 9 节「训练建议」抓 ≤3 条 bullet。

    锚点变体：## 九、训练建议 / ## 9. 训练建议 / ## 训练建议
    bullet 形如：- xxx / * xxx / • xxx
    缺节时返回 []。
    """
    if not report_md:
        return []
    import re as _re
    m = _re.search(
        r"^#{2,4}\s*[（(]?[一二三四五六七八九十\d]+[)）、\.]?\s*训练建议.*?$",
        report_md, _re.M,
    )
    if not m:
        m = _re.search(r"^#{2,4}\s*[（(]?\d+[)）]?\s*[^\n]*建议[^\n]*$", report_md, _re.M)
        if not m:
            return []
    body = report_md[m.end():]
    end_m = _re.search(r"^#{2,3}\s+[一二三四五六七八九十\d]", body, _re.M)
    section = body[: end_m.start() if end_m else len(body)]
    bullets = _re.findall(r"^\s*[-*•]\s+(.+?)\s*$", section, _re.M)
    cleaned = []
    for b in bullets[:3]:
        text = _re.sub(r"\*\*?(.+?)\*\*?", r"\1", b)
        text = _re.sub(r"\s+", " ", text).strip()
        if text:
            cleaned.append(text)
    return cleaned


def _extract_ai_evaluation_from_report(report_md: str) -> dict:
    """从 report.md 抽取 AI 测评内容（v3.6 · 关键修复）

    报告结构（多种变体）：

      【变体 1】完整版（陈豆豆）：
        ### 5. 【考纲精准定级与知识点盲区】
        根据NOI 2025大纲和你的知识点覆盖，你的真实水平为：**【提高级 (CSP-S) 门槛级】** 或 **入门级 (CSP-J) 资深级**。
        #### 核心解读
        <p class="text-blue-700 font-semibold">核心解读：xxx</p>
        ### 7. 【代码质量与工程习惯深度分析】
        **综合评价**：xxx

      【变体 2】标准版（大多数报告）：
        ### 5. 【考纲精准定级与知识点盲区】
        <p class="text-blue-700 font-semibold">定级：xxx，你目前处于【CSP-J 入门级】水平...</p>
        ### 9. 【核心建议（优先级排序）】
        * 🟢 **建议: ...**

      【变体 3】极简版（早期报告）：
        <p>你的真实水平处于【xxx】阶段</p>

    返回 dict：
      - ai_level: AI 定级（如 "CSP-S 门槛级"），未找到 → None
      - core_reading: 核心解读/综合评价（一段 1-2 句），未找到 → 退化为核心建议
      - verdict: 综合评价（一段），未找到 → None
      - report_date: 报告生成时间（"最后更新"用），未找到 → None
    """
    import re
    out = {
        "ai_level": None,
        "core_reading": None,
        "verdict": None,
        "report_date": None,
    }
    if not report_md:
        return out

    # ─── 1) AI 定级 ────────────────────────────────────
    # 优先：粗体括号 【xxx】（格式 1）
    for pat in [
        r"你的真实水平为：\*\*【(.+?)】\*\*",                # 格式 1
        r"你目前的真实水平[，,。：:].{0,50}?【(.+?)】",       # 格式 2
        r"定级[：:].{0,80}?【(.+?)】",                         # 格式 2 alt
        r"真实水平[，,。：:].{0,80}?【(.+?)】",                # 通用
        r"结论[：:].{0,80}?【(.+?)】",                         # 变体
        r"处于【(.+?)】(?:阶段|水平|门槛)",                    # 简写
    ]:
        m = re.search(pat, report_md)
        if m:
            out["ai_level"] = m.group(1).strip()
            break

    # ─── 2) 核心解读 / 综合评价 ──────────────────────
    # 优先：<p class="text-blue-700 font-semibold">核心解读：xxx</p>
    m = re.search(
        r'<p[^>]*>\s*(?:核心解读|核心评价|综合点评)[：:]\s*(.+?)\s*</p>',
        report_md, re.S,
    )
    if m:
        text = re.sub(r"\*\*?", "", m.group(1)).strip()
        if len(text) > 140:
            text = text[:140].rstrip() + "…"
        out["core_reading"] = text

    # 备选：裸的 "**综合评价**：xxx"
    if not out["core_reading"]:
        m = re.search(r"\*\*综合评价[：:]\*\*\s*(.+?)(?:\n\n|\n####|\n---)", report_md, re.S)
        if m:
            text = m.group(1).strip()
            if len(text) > 140:
                text = text[:140].rstrip() + "…"
            out["core_reading"] = text

    # 再备选：第 9 节"核心建议"第 1 条
    if not out["core_reading"]:
        m = re.search(
            r"###\s*\d+\.\s*【核心建议[（(].*?[)）].*?】.*?(?:\n|\*\*)",
            report_md,
        )
        if m:
            # 取该 section 下的第 1 条
            section_start = m.end()
            # 跳到第 1 个 * / - 后
            rest = report_md[section_start:section_start + 800]
            item = re.search(r"^\s*[\*\-]\s*(?:🔴|🟡|🟢)?\s*\*?\*?建议[：:]?\s*\*?\*?\s*(.+)", rest, re.M)
            if item:
                text = item.group(1).strip()[:140]
                if len(text) == 140:
                    text += "…"
                out["core_reading"] = text

    # ─── 3) 报告生成时间 ──────────────────────────────
    m = re.search(r"报告生成时间[：:]\s*(\d{4}-\d{2}-\d{2}\s*\d{2}:\d{2})", report_md)
    if m:
        out["report_date"] = m.group(1)

    return out


def _build_share_card_data(luogu_uid: str) -> dict | None:
    """组装"9 月我家孩子位置"分享卡所需数据

    字段：
      · name: 学员姓名（缺省 "学员"）
      · uid: 洛谷 UID
      · gesp_level / gesp_score: 当前 GESP 最高级 / 最近分
      · segment: 8 段位字符串（如 "1✦ 2★ 3□ 4□ 5✦ 6★ 7□ 8□"）
      · events: 关键赛事倒计时 [{name, date, days}]
      · can_j / can_s: 是否已可免 CSP-J / CSP-S 初赛
      · gap_j / gap_s: 距免初赛的差距
      · last_exam: 最近一次 GESP 真考 (level, score, year)
      · asof: "最后更新" 日期
    """
    from datetime import date as _date
    from docs.gesp_estimator import compute_exemptions

    student = _admin_students.get_student_by_uid(luogu_uid)
    if not student:
        return None

    gesp_level = int(student.get("gesp_highest_passed") or 0)
    gesp_score = int(student.get("gesp_latest_score") or 0)
    name = (student.get("real_name") or "").strip() or "学员"

    def _mark(lv: int) -> str:
        """段位字符：80+ ✦ / 60-79 ★ / 未通过或未考 □"""
        if lv <= gesp_level:
            if gesp_score >= 80:
                return "✦"
            if gesp_score >= 60:
                return "★"
            return "✗"
        return "□"

    segment = "  ".join(f"{i}{_mark(i)}" for i in range(1, 9))

    # 关键赛事（2026 + 未过期 + GESP/CSP/NOIP/NOI）
    try:
        from task_store import _get_conn
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT name, exam_date FROM competitions "
                "WHERE data_year = 2026 "
                "  AND exam_date >= date('now', '-7 days') "
                "  AND (name LIKE '%GESP%' OR name LIKE '%CSP%' "
                "       OR name LIKE '%NOIP%' OR name LIKE '%NOI%') "
                "ORDER BY exam_date LIMIT 8"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        rows = []

    today = _date.today()
    events = []
    for ename, edate in rows:
        try:
            d = _date.fromisoformat(edate)
            events.append({
                "name": ename,
                "display": _shorten_comp_name(ename),  # 海报可读名（保留 GESP/CSP-J/S/NOIP/NOI）
                "date": edate,
                "days": max(0, (d - today).days),
            })
        except Exception:
            pass

    exemptions = compute_exemptions(gesp_level, gesp_score) if gesp_level else []
    can_j = "csp_j" in exemptions
    can_s = "csp_s" in exemptions

    if not gesp_level:
        gap_j = "未参加 GESP"
        gap_s = "未参加 GESP"
    elif can_j:
        gap_j = "✅ 已可免 CSP-J 初赛"
    else:
        gap_j = f"差 {max(0, 7 - gesp_level)} 级 + {max(0, 80 - gesp_score)} 分"
    if not gesp_level:
        gap_s = "未参加 GESP"
    elif can_s:
        gap_s = "✅ 已可免 CSP-S 初赛"
    else:
        gap_s = f"差 {max(0, 8 - gesp_level)} 级 + {max(0, 80 - gesp_score)} 分"

    # 最近一次 GESP 真考
    last_exam = None
    try:
        from task_store import _get_conn
        conn = _get_conn()
        try:
            r = conn.execute(
                "SELECT level, score, award_year FROM gesp_exams "
                "WHERE student_id = ? ORDER BY award_year DESC, id DESC LIMIT 1",
                (int(student["id"]),),
            ).fetchone()
            if r:
                last_exam = {"level": r[0], "score": r[1], "year": r[2]}
        finally:
            conn.close()
    except Exception:
        pass

    # ── v3.6 关键：从最新 report.md 抽取 AI 测评（用户原话："测评结果应该来源于报告"） ──
    ai_eval = {
        "ai_level": None,
        "core_reading": None,
        "verdict": None,
        "report_date": None,
    }
    report_assets: dict = {}  # 性格雷达 / 标签图（用于海报主视觉替代 AI 核心解读）
    report_dir_path = None
    try:
        report_dir = _find_latest_report_dir(luogu_uid, name)
        if report_dir is not None:
            report_dir_path = report_dir
            md_path = report_dir / "report.md"
            if md_path.exists():
                report_md = md_path.read_text(encoding="utf-8", errors="replace")
                ai_eval = _extract_ai_evaluation_from_report(report_md)
            # v3.6.1 · 海报要展示的图表（来自报告 assets/）
            for key, fname in [
                ("personality_radar", "personality_radar.png"),
                ("top_tags", "top_tags.png"),
            ]:
                p = report_dir / "assets" / fname
                if p.exists():
                    report_assets[key] = str(p)
    except Exception:
        pass

    # 报告生成时间若存在则覆盖 asof
    asof = today.strftime("%Y-%m-%d")
    if ai_eval.get("report_date"):
        asof = ai_eval["report_date"].split(" ")[0]

    return {
        "name": name,
        "uid": luogu_uid,
        "gesp_level": gesp_level,
        "gesp_score": gesp_score,
        "segment": segment,
        "events": events,
        "can_j": can_j,
        "can_s": can_s,
        "gap_j": gap_j,
        "gap_s": gap_s,
        "last_exam": last_exam,
        "asof": asof,
        # v3.6 AI 测评（来自 report.md）
        "ai_level": ai_eval.get("ai_level"),
        "core_reading": ai_eval.get("core_reading"),
        "verdict": ai_eval.get("verdict"),
        # v3.6.1 报告图表（来自 report assets/，用于海报主视觉替代 AI 核心解读文字）
        "report_assets": report_assets,
    }


def _ai_evaluation(data: dict) -> tuple[str, str]:
    """根据 GESP 等级 + 分数生成 1 句 AI 评估 + 1 个标签

    返回 (评估语, 评估标签 [强/中/弱])
    """
    lv = data.get("gesp_level") or 0
    sc = data.get("gesp_score") or 0
    if lv == 0:
        return ("建议从 GESP 1 级起步，9 月前冲到 7 级有机会免 CSP-J 初赛", "起步")
    if sc >= 90:
        return (f"AI 评估：高分 {sc} 分，算法基础扎实，可冲 NOI 决赛梯队", "强")
    if sc >= 80:
        if lv >= 7:
            return (f"AI 评估：高分 {sc} 分，已可免 CSP-J 初赛；下一目标 8 级 80+ 免 CSP-S", "强")
        return (f"AI 评估：高分 {sc} 分，可尝试跳级 → 目标 {lv+2} 级", "强")
    if sc >= 60:
        if lv >= 7:
            return (f"AI 评估：通过 {sc} 分，距免初赛只差 1 级，6 月可冲 8 级 60+", "中")
        return (f"AI 评估：通过 {sc} 分，建议巩固 {lv} 级 → 下一目标 {lv+1} 级 80+", "中")
    return (f"AI 评估：{sc} 分未达 60，建议重考 {lv} 级巩固基础", "弱")


def _render_share_card_png(data: dict, qr_url: str) -> bytes:
    """v3.6 传播期 · 信息学 AI 测评结果海报（PNG）· 重设计版

    视觉定位：**信息学 AI 测评结果** 卡片（不是赛事日历）
    900×1600 纵向海报（4:7 比例，适合朋友圈/小红书）
    关键变化（v3.5.2 → v3.6）：
      · 标题：AI 测评结果 → 信息学 AI 测评结果
      · 主内容：测评结果改为来自 report.md 的 AI 定级 + 核心解读
        · GESP 等级 / 分数 降级为小事实（"事实"标签）
      · 删除学员信息行中多余的紫色头像占位
    布局（自上而下）：
      ① 顶部渐变带："信息学 AI 测评结果" 大标题 + 副标题
      ② 学员信息行（姓名 + UID）
      ③ **主视觉：AI 测评结果大面板** —— AI 定级 + 核心解读
      ④ GESP 真考事实条（已通过级 / 最近分）
      ⑤ 免初赛状态条（CSP-J / CSP-S 双行）
      ⑥ 8 段位进度条（圆点 + 连接线）
      ⑦ 关键赛事倒计时列表（最多 5 场）
      ⑧ 底部：QR 码 + 网址 + 品牌水印
    """
    import io
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch, Circle
    import matplotlib.patheffects as pe
    import qrcode
    from qrcode.image.pure import PyPNGImage

    # 中文字体（Windows 优先；其他平台降级）
    # 注意：matplotlib 渲染 emoji 会失败（普通 TrueType 字体不含 emoji 字形）
    # 本设计完全使用 ASCII 符号 + 几何形状，无需 emoji 字体
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    # ── 主色板 ──────────────
    COLOR_BG = "#F5F3FF"          # 整张背景：淡紫
    COLOR_PRIMARY = "#6366F1"     # 主紫
    COLOR_PRIMARY_DK = "#4F46E5"  # 深紫
    COLOR_PRIMARY_LT = "#A5B4FC"  # 浅紫
    COLOR_ACCENT = "#8B5CF6"      # 辅紫
    COLOR_GREEN = "#10B981"       # 免初赛
    COLOR_AMBER = "#F59E0B"       # 中评
    COLOR_RED = "#EF4444"         # 弱评
    COLOR_GRAY = "#6B7280"        # 起步
    COLOR_TEXT = "#0F172A"        # 主文字
    COLOR_TEXT_LT = "#64748B"     # 次文字
    COLOR_TEXT_XL = "#94A3B8"     # 浅文字
    COLOR_CARD = "#FFFFFF"        # 卡片白
    COLOR_CARD_EDGE = "#E0E7FF"   # 卡片边

    # 画布 9 × 16，竖版海报
    fig = plt.figure(figsize=(9, 16), dpi=110)
    fig.patch.set_facecolor(COLOR_BG)
    ax = fig.add_subplot(111)
    ax.set_xlim(0, 9)
    ax.set_ylim(0, 16)
    ax.set_aspect("equal")
    ax.axis("off")

    def _rounded(x, y, w, h, fc, ec="none", lw=0, r=0.18):
        return FancyBboxPatch(
            (x, y), w, h, boxstyle=f"round,pad=0.0,rounding_size={r}",
            linewidth=lw, edgecolor=ec, facecolor=fc
        )

    # 顶部 紫色渐变带（手动堆叠 4 层模拟渐变） ──────────────
    grad_strips = [
        (0.0, 14.6, 9.0, 1.4, COLOR_PRIMARY_DK),
        (0.0, 14.2, 9.0, 0.4, "#5E50E8"),
        (0.0, 13.9, 9.0, 0.3, "#6E60EB"),
    ]
    for x, y, w, h, c in grad_strips:
        ax.add_patch(_rounded(x, y, w, h, c, r=0.0))
    # 装饰小圆点
    for cx, cy, r, a in [(0.8, 15.4, 0.10, 0.18), (8.2, 15.0, 0.15, 0.13),
                          (1.5, 14.9, 0.06, 0.35), (7.6, 15.5, 0.08, 0.25)]:
        ax.add_patch(Circle((cx, cy), r, facecolor="white", alpha=a, edgecolor="none"))

    # 主标题"信息学AI测评结果"（最大字号）
    ax.text(4.5, 15.0, "信息学AI测评结果", ha="center", va="center",
            fontsize=32, color="white", fontweight="bold",
            path_effects=[pe.withStroke(linewidth=1.5, foreground=COLOR_PRIMARY_DK)])
    # 副标题：AI 测评编程能力报告 · 基于选手洛谷数据
    ax.text(4.5, 14.45, "AI测评编程能力报告-基于选手洛谷数据",
            ha="center", va="center", fontsize=12, color="#E0E7FF")

    # ── 学员信息行 ──────────────
    ax.add_patch(_rounded(0.5, 13.55, 8.0, 0.55, COLOR_CARD, ec=COLOR_CARD_EDGE, lw=1, r=0.25))
    ax.text(0.85, 13.83, data['name'], ha="left", va="center",
            fontsize=14, color=COLOR_TEXT, fontweight="bold")
    ax.text(8.2, 13.83, f"UID  {data['uid']}", ha="right", va="center",
            fontsize=11, color=COLOR_TEXT_LT, family="monospace")

    # ── ★ 主视觉：AI 测评结果面板（v3.6 · 来自 report.md） ★ ──────────────
    ai_level = data.get("ai_level")        # 如 "CSP-S 门槛级"
    core_reading = data.get("core_reading")  # 一段话
    eval_text, eval_tag = _ai_evaluation(data)
    badge_color = {"强": COLOR_GREEN, "中": COLOR_AMBER, "弱": COLOR_RED, "起步": COLOR_GRAY}.get(eval_tag, COLOR_GRAY)
    badge_label = {"强": "能力较强", "中": "能力中等", "弱": "待提升", "起步": "起步阶段"}.get(eval_tag, eval_tag)

    # 卡片主体
    ax.add_patch(_rounded(0.5, 8.30, 8.0, 5.00, COLOR_CARD, ec=COLOR_PRIMARY_LT, lw=2, r=0.25))
    # ─ 第一行：AI 定级 label (左) + 评级徽章 (右)，同行水平 ──────
    ax.text(0.85, 12.55, "AI 定级", ha="left", va="center",
            fontsize=12, color=COLOR_TEXT_LT, fontweight="bold")
    ax.add_patch(_rounded(6.55, 12.32, 1.85, 0.45, badge_color, r=0.20))
    ax.text(7.475, 12.55, f"●  {badge_label}", ha="center", va="center",
            fontsize=11, color="white", fontweight="bold")

    # ─ AI 定级（大字，主视觉） ──────────────
    if ai_level:
        # 取定级中的关键名词（CSP-S 门槛级 / 入门级 资深级 / 普及级 等）
        level_text = ai_level
        # 限长，防止溢出
        if len(level_text) > 12:
            level_text = level_text[:12]
    else:
        level_text = "尚未生成报告"

    # 大字（label/大字/小标签 三组留白均衡）
    ax.text(0.85, 11.85, level_text, ha="left", va="center",
            fontsize=28, color=COLOR_PRIMARY_DK, fontweight="bold",
            path_effects=[pe.withStroke(linewidth=0.4, foreground=COLOR_PRIMARY_LT)])
    # 小标签
    if ai_level:
        ax.text(0.85, 11.20, "（基于 NOI 2025 大纲 · AI 综合判定）",
                ha="left", va="center", fontsize=9.5, color=COLOR_TEXT_XL, style="italic")

    # 分隔线
    ax.plot([0.85, 8.15], [10.80, 10.80], color=COLOR_CARD_EDGE, linewidth=1, linestyle="--")

    # ── v3.6.1 主视觉下半区：性格画像雷达 + 高频算法标签（来自 report assets/） ──────────
    # 替代原本的"AI 核心解读"文字区，让测评结果以图表形式更直观
    report_assets = data.get("report_assets") or {}
    personality_path = report_assets.get("personality_radar")
    toptags_path = report_assets.get("top_tags")

    # 两条小标头（与主面板同色，对应两张图）
    ax.add_patch(_rounded(0.85, 10.50, 0.18, 0.22, COLOR_PRIMARY, r=0.05))
    ax.text(1.13, 10.61, "AI 性格画像", ha="left", va="center",
            fontsize=12, color=COLOR_PRIMARY_DK, fontweight="bold")
    ax.add_patch(_rounded(4.85, 10.50, 0.18, 0.22, COLOR_PRIMARY, r=0.05))
    ax.text(5.13, 10.61, "AI 高频算法", ha="left", va="center",
            fontsize=12, color=COLOR_PRIMARY_DK, fontweight="bold")

    # ─ 图区：左 性格画像（extent x: 0.70~4.30, y: 8.45~10.10）  ──
    # 右 高频标签（extent x: 4.70~8.30, y: 8.45~10.10）
    IMG_LEFT_X0, IMG_LEFT_X1 = 0.70, 4.30
    IMG_RIGHT_X0, IMG_RIGHT_X1 = 4.70, 8.30
    IMG_Y0, IMG_Y1 = 8.45, 10.10

    if personality_path and os.path.exists(personality_path):
        try:
            img_l = plt.imread(personality_path)
            ax.imshow(img_l, extent=(IMG_LEFT_X0, IMG_LEFT_X1, IMG_Y0, IMG_Y1),
                      aspect="auto", zorder=2)
        except Exception:
            ax.text((IMG_LEFT_X0 + IMG_LEFT_X1) / 2, (IMG_Y0 + IMG_Y1) / 2,
                    "（性格画像加载失败）", ha="center", va="center",
                    fontsize=9.5, color=COLOR_TEXT_XL, style="italic")
    else:
        ax.text((IMG_LEFT_X0 + IMG_LEFT_X1) / 2, (IMG_Y0 + IMG_Y1) / 2,
                "（尚未生成报告，无性格画像）", ha="center", va="center",
                fontsize=9.5, color=COLOR_TEXT_XL, style="italic")

    if toptags_path and os.path.exists(toptags_path):
        try:
            img_r = plt.imread(toptags_path)
            ax.imshow(img_r, extent=(IMG_RIGHT_X0, IMG_RIGHT_X1, IMG_Y0, IMG_Y1),
                      aspect="auto", zorder=2)
        except Exception:
            ax.text((IMG_RIGHT_X0 + IMG_RIGHT_X1) / 2, (IMG_Y0 + IMG_Y1) / 2,
                    "（算法标签加载失败）", ha="center", va="center",
                    fontsize=9.5, color=COLOR_TEXT_XL, style="italic")
    else:
        ax.text((IMG_RIGHT_X0 + IMG_RIGHT_X1) / 2, (IMG_Y0 + IMG_Y1) / 2,
                "（尚未生成报告，无算法标签）", ha="center", va="center",
                fontsize=9.5, color=COLOR_TEXT_XL, style="italic")

    # ── GESP 真考事实条（v3.6 · 降级为"事实"，不再作为测评结果） ──────────────
    # 黄色信息条，标识 GESP 仅为真考事实数据，非 AI 测评结论
    ax.add_patch(_rounded(0.7, 8.00, 7.6, 0.20, "#FEF3C7", r=0.10))
    ax.text(4.5, 8.00, "事实数据  ·  GESP 真考成绩",
            ha="center", va="center", fontsize=9, color="#92400E", fontweight="bold")

    # 两条事实
    if data["gesp_level"] > 0:
        fact_l = f"已通过 GESP {data['gesp_level']} 级"
        fact_l_color = COLOR_TEXT
    else:
        fact_l = "尚未参加 GESP"
        fact_l_color = COLOR_TEXT_XL
    if data["gesp_level"] > 0:
        if data["gesp_score"] >= 80:
            fact_r = f"最近分 {data['gesp_score']} / 100（高分）"
            fact_r_color = COLOR_GREEN
        elif data["gesp_score"] >= 60:
            fact_r = f"最近分 {data['gesp_score']} / 100（通过）"
            fact_r_color = COLOR_AMBER
        else:
            fact_r = f"最近分 {data['gesp_score']} / 100（未达 60）"
            fact_r_color = COLOR_RED
    else:
        fact_r = "—"
        fact_r_color = COLOR_TEXT_XL
    ax.text(0.85, 7.65, fact_l, ha="left", va="center",
            fontsize=11, color=fact_l_color, fontweight="bold")
    ax.text(8.15, 7.65, fact_r, ha="right", va="center",
            fontsize=11, color=fact_r_color, fontweight="bold")

    # ── 免初赛状态条 ──────────────
    # 用紫色小方块代替 emoji（v3.6 · 下移 1.25 让位给扩大的 AI 主面板）
    ax.add_patch(_rounded(4.5 - 0.85, 7.00, 0.16, 0.20, COLOR_PRIMARY_DK, r=0.05))
    ax.text(4.5 + 0.05, 7.10, "9 月免初赛状态", ha="center", va="center",
            fontsize=12, color=COLOR_PRIMARY_DK, fontweight="bold")
    if data["can_j"] or data["can_s"]:
        j_label = f"【免】已可免 CSP-J 初赛" if data["can_j"] else data["gap_j"]
        s_label = f"【免】已可免 CSP-S 初赛" if data["can_s"] else data["gap_s"]
        j_color = COLOR_GREEN if data["can_j"] else COLOR_TEXT
        s_color = COLOR_GREEN if data["can_s"] else COLOR_TEXT
    else:
        j_label = data["gap_j"]
        s_label = data["gap_s"]
        j_color = s_color = COLOR_TEXT
    # J 行
    ax.add_patch(_rounded(0.7, 6.40, 7.6, 0.45, COLOR_CARD, ec=COLOR_CARD_EDGE, lw=1, r=0.20))
    ax.text(0.95, 6.625, "CSP-J  普及组", ha="left", va="center",
            fontsize=11, color=COLOR_TEXT_LT, fontweight="bold")
    ax.text(8.05, 6.625, j_label, ha="right", va="center",
            fontsize=11, color=j_color, fontweight="bold")
    # S 行
    ax.add_patch(_rounded(0.7, 5.85, 7.6, 0.45, COLOR_CARD, ec=COLOR_CARD_EDGE, lw=1, r=0.20))
    ax.text(0.95, 6.075, "CSP-S  提高组", ha="left", va="center",
            fontsize=11, color=COLOR_TEXT_LT, fontweight="bold")
    ax.text(8.05, 6.075, s_label, ha="right", va="center",
            fontsize=11, color=s_color, fontweight="bold")

    # ── 段位图（圆点 + 连接线） ──────────────
    # 用紫色方块代替 emoji
    ax.add_patch(_rounded(4.5 - 1.20, 5.20, 0.16, 0.20, COLOR_PRIMARY_DK, r=0.05))
    ax.text(4.5 + 0.10, 5.30, "GESP 8 段位进度", ha="center", va="center",
            fontsize=12, color=COLOR_PRIMARY_DK, fontweight="bold")
    seg_y = 4.55
    seg_left, seg_right = 0.7, 8.3
    seg_w = seg_right - seg_left
    n = 8
    step = seg_w / (n - 1)
    # 连接线
    for i in range(n - 1):
        x1 = seg_left + i * step
        x2 = seg_left + (i + 1) * step
        passed = (i + 1) <= data["gesp_level"]
        ax.plot([x1, x2], [seg_y, seg_y],
                color=COLOR_PRIMARY if passed else "#CBD5E1",
                linewidth=3, solid_capstyle="round")
    # 圆点 + 等级
    for i in range(1, n + 1):
        x = seg_left + (i - 1) * step
        if i <= data["gesp_level"]:
            mark_color = COLOR_PRIMARY
            mark_edge = "white"
            mark_size = 0.18
            text_color = COLOR_PRIMARY_DK
            text_weight = "bold"
        else:
            mark_color = "white"
            mark_edge = "#CBD5E1"
            mark_size = 0.14
            text_color = COLOR_TEXT_XL
            text_weight = "normal"
        ax.add_patch(Circle((x, seg_y), mark_size, facecolor=mark_color,
                            edgecolor=mark_edge, linewidth=2.5))
        ax.text(x, seg_y + 0.35, f"{i}", ha="center", va="center",
                fontsize=11, color=text_color, fontweight=text_weight)
        if i == data["gesp_level"] and data["gesp_level"] > 0:
            ax.text(x, seg_y - 0.40, "当前", ha="center", va="center",
                    fontsize=9, color=badge_color, fontweight="bold")

    # ── 关键赛事倒计时 ──────────────
    y = 3.95
    # 用紫色方块代替 emoji
    ax.add_patch(_rounded(4.5 - 1.10, 3.85, 0.16, 0.20, COLOR_PRIMARY_DK, r=0.05))
    ax.text(4.5 + 0.20, 3.95, "2026 关键赛事倒计时", ha="center", va="center",
            fontsize=12, color=COLOR_PRIMARY_DK, fontweight="bold")
    y -= 0.40
    visible_events = (data.get("events") or [])[:3]
    for ev in visible_events:
        days = ev["days"]
        if days <= 0:
            tag = "进行中"
            tag_color = COLOR_RED
            tag_bg = "#FEE2E2"
        elif days <= 14:
            tag = f"! {days} 天"
            tag_color = "white"
            tag_bg = COLOR_RED
        elif days <= 60:
            tag = f"还有 {days} 天"
            tag_color = COLOR_AMBER
            tag_bg = "#FEF3C7"
        else:
            tag = f"{days} 天后"
            tag_color = COLOR_TEXT_LT
            tag_bg = "#F1F5F9"
        nm = ev.get("display") or ev["name"]
        # 赛事行
        ax.add_patch(_rounded(0.7, y - 0.18, 7.6, 0.40, COLOR_CARD,
                              ec=COLOR_CARD_EDGE, lw=1, r=0.15))
        ax.text(0.95, y + 0.02, f"•  {nm}", ha="left", va="center",
                fontsize=10.5, color=COLOR_TEXT, fontweight="bold")
        # 倒计时徽章
        ax.add_patch(_rounded(6.55, y - 0.08, 1.65, 0.30, tag_bg, r=0.15))
        ax.text(7.375, y + 0.07, tag, ha="center", va="center",
                fontsize=9.5, color=tag_color, fontweight="bold")
        y -= 0.40
    if not visible_events:
        ax.text(4.5, y, "（暂无即将到来的赛事）", ha="center", va="center",
                fontsize=11, color=COLOR_TEXT_XL)
        y -= 0.4

    # 备注脚注（固定 y=2.30，赛事行最底 y=2.75 → 顶 2.97，间距 0.50+）
    y_foot = 2.30
    ax.text(4.5, y_foot, "CSP-J/S = CCF 软件能力认证 · NOIP = 信息学奥赛联赛 · NOI = 信息学奥赛决赛",
            ha="center", va="center", fontsize=8, color=COLOR_TEXT_XL, style="italic")

    # ── QR 码 + URL ──────────────
    qr = qrcode.QRCode(version=2, box_size=8, border=2, image_factory=PyPNGImage)
    qr.add_data(qr_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")
    qr_buf = io.BytesIO()
    qr_img.save(qr_buf, "PNG")
    qr_buf.seek(0)

    from PIL import Image as _PILImage
    qr_pil = _PILImage.open(qr_buf).convert("RGBA")
    qr_target = 1.5
    # 用 ax.imshow 直接以 ax 数据坐标定位（不受 figure 边距影响）
    # 居中于 QR 白底 (5.55, 0.40, 2.85, 2.10) — 中心 (6.975, 1.45)
    qr_left = 6.975 - qr_target / 2
    qr_bottom = 1.45 - qr_target / 2
    ax.imshow(qr_pil, extent=[qr_left, qr_left + qr_target,
                                qr_bottom, qr_bottom + qr_target],
              aspect="equal", zorder=3, interpolation="nearest")

    # QR 白底（与左侧文字同高，QR 码居中）
    ax.add_patch(_rounded(5.55, 0.40, 2.85, 2.10, COLOR_CARD,
                          ec=COLOR_CARD_EDGE, lw=1, r=0.20))
    # 左侧文字（与 QR 白底顶部对齐；脚注 y=2.30，"扫码..." y=2.00 距脚注 0.30）
    # 用紫色方块代替 📱 emoji
    ax.add_patch(_rounded(0.50, 1.89, 0.22, 0.22, COLOR_PRIMARY, r=0.05))
    ax.text(0.61, 2.00, "Q", ha="center", va="center",
            fontsize=12, color="white", fontweight="bold")
    ax.text(0.85, 2.00, "扫码查看完整 AI 测评",
            ha="left", va="center", fontsize=13, color=COLOR_TEXT, fontweight="bold")
    ax.text(0.50, 1.60, "免费 AI 测评 · 1 分钟出报告",
            ha="left", va="center", fontsize=10, color=COLOR_TEXT_LT)
    ax.text(0.50, 1.10, qr_url, ha="left", va="center",
            fontsize=8, color=COLOR_TEXT_LT, family="monospace")
    ax.text(0.50, 0.80, "● AI 估算 · 不替代真考",
            ha="left", va="center", fontsize=8.5, color=COLOR_TEXT_XL)
    ax.text(0.50, 0.50, f"● 最后更新 {data['asof']}",
            ha="left", va="center", fontsize=8.5, color=COLOR_TEXT_XL)

    # ── 输出 PNG ──────────────
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor(), dpi=120)
    plt.close(fig)
    return buf.getvalue()


@app.route("/me/<luogu_uid>/share-card.png", methods=["GET"])
def share_card_png(luogu_uid: str):
    """v3.5.2 传播期 · 位置图 PNG（学员自助中心"生成"按钮所调）"""
    data = _build_share_card_data(luogu_uid)
    if not data:
        return "UID 未注册", 404
    base = request.host_url.rstrip("/")
    qr_url = f"{base}/me/{luogu_uid}"
    png_bytes = _render_share_card_png(data, qr_url)
    return Response(png_bytes, mimetype="image/png", headers={
        "Content-Disposition": f'inline; filename="share-card-{luogu_uid}.png"',
        "Cache-Control": "public, max-age=600",
    })


# ---- v3.5.2 · 家长订阅版（5 维度深度分析） ----

def _build_parent_subscribe_data(student: dict, luogu_uid: str) -> dict:
    """组装家长订阅版所需的全部数据：5 维度"""
    import json as _json
    from datetime import date as _date
    from docs.gesp_estimator import compute_exemptions, next_eligible_gesp_level

    sid = int(student.get("id") or 0)
    student_d = dict(student)
    student_d["province"] = _city_to_province(student_d.get("city"))
    student_d["grade_label"] = _grade_to_label(student_d.get("grade"))

    gesp_level = int(student_d.get("gesp_highest_passed") or 0)
    gesp_score = int(student_d.get("gesp_latest_score") or 0)
    next_lv = int(student_d.get("gesp_next_eligible_level") or 1)
    exemptions = compute_exemptions(gesp_level, gesp_score) if gesp_level else []

    # ---- 维度 3 · GESP 跳级 + 免初赛 ----
    # 距离下一个等级还需要多少分（AI 估算，永远带"AI 估算"水印）
    gesp_gap = max(0, 60 - gesp_score) if gesp_level else 60
    can_exempt_cspj = gesp_level >= 7 and gesp_score >= 80
    can_exempt_csps = gesp_level >= 8 and gesp_score >= 80

    # ---- 维度 1 · OI 生涯倒推（3 档时间线） ----
    # 简化算法：按当前段位 + 假设每月 1 个级别
    target = "省一"  # 默认目标
    if can_exempt_csps:
        target = "NOI 金牌 / 国家集训队"
        timeline = {
            "conservative": "24 个月（保底路线：CSP-S 一等 → NOIP → NOI 银牌）",
            "aggressive": "12 个月（CSP-S 一等 → NOI 银牌冲刺）",
            "fallback": "保底：CSP-S 二等 + 强基破格",
        }
    elif can_exempt_cspj or (gesp_level >= 7 and gesp_score >= 60):
        target = "省一 / NOI 银牌"
        timeline = {
            "conservative": "18 个月（CSP-J 一等 → CSP-S → NOIP）",
            "aggressive": "10 个月（CSP-S 一等 → NOIP 200 分）",
            "fallback": "保底：CSP-J 一等 + 强基/综合评价",
        }
    elif gesp_level >= 4:
        target = "GESP 7 级 80+（免 CSP-J 初赛）"
        timeline = {
            "conservative": "12 个月（GESP 5→6→7 级，每级 80+）",
            "aggressive": "6 个月（跳级 GESP 5→7，需要 90+）",
            "fallback": "保底：CSP-J 三等 + 综合评价",
        }
    else:
        target = "GESP 4 级 60+（第一个通过级别）"
        timeline = {
            "conservative": "8 个月（GESP 1→2→3→4 级，稳扎稳打）",
            "aggressive": "4 个月（GESP 1→3 跳级）",
            "fallback": "保底：GESP 2 级通过 + 校内推荐",
        }

    # ---- 维度 2 · 政策时间线（读 competitions.json） ----
    policy_events = []
    try:
        comp_path = Path(__file__).parent / "docs" / "competitions.json"
        if comp_path.exists():
            data = _json.loads(comp_path.read_text(encoding="utf-8"))
            today = _date.today()
            for ev in (data.get("policy_events") or data.get("competitions") or []):
                d_str = ev.get("date") or ""
                if not d_str:
                    continue
                try:
                    d = _date.fromisoformat(d_str)
                except Exception:
                    continue
                days_left = (d - today).days
                if -180 < days_left < 730:  # 显示半年内到未来 2 年
                    policy_events.append({
                        "name": ev.get("name") or ev.get("title") or "—",
                        "date": d_str,
                        "days_left": days_left,
                        "category": ev.get("category", "政策"),
                        "summary": ev.get("summary") or ev.get("description") or "",
                    })
            policy_events.sort(key=lambda x: x["days_left"])
    except Exception:
        pass

    # ---- 维度 4 · 学员当前状态（从 progress 提取） ----
    progress = _admin_students.get_student_gesp_progress(sid) or {}
    exams = progress.get("exams") or progress.get("history") or []
    last_exam = exams[-1] if exams else None
    # 难度分布（从最近一份 report.md 抓，不强求）
    diff_dist = {}
    try:
        # 找该学员最近一份报告
        reports_root = Path(__file__).parent / "reports"
        candidates = sorted(
            [d for d in reports_root.iterdir() if d.is_dir() and str(luogu_uid) in d.name],
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        ) if reports_root.exists() else []
        if candidates:
            md = (candidates[0] / "report.md").read_text(encoding="utf-8") if (candidates[0] / "report.md").exists() else ""
            import re as _re
            m = _re.search(r"难度分布[^\n]*\n.*?\n([\d/\s]+)", md)
            if m:
                nums = _re.findall(r"\d+", m.group(1))
                for i, lvl in enumerate(["入门", "普及", "提高", "省选", "NOI"], 0):
                    if i < len(nums):
                        diff_dist[lvl] = int(nums[i])
    except Exception:
        pass

    # ---- 维度 5 · 教练沟通清单（7 个开放问题，模板填充） ----
    questions = [
        f"我家孩子（UID {luogu_uid}）目前在 CSP-J 普及组中处于第几梯队？",
        f"按当前做题曲线，未来 6 个月达到 GESP {min(8, next_lv + 1)} 级 80+ 的概率有多大？",
        f"如果想冲省一，应该在哪个时间点切换赛道路线（CSP-J → CSP-S）？",
        f"最近 30 天错题主要集中在哪些算法标签？这是否反映系统性薄弱？",
        f"我们家长是否需要报名某个专项集训（贪心/DP/图论）来补强？",
        f"按 GESP/CSP/NOIP 节奏，孩子的 OI 路径与中考/高考时间是否冲突？",
        f"教练建议的每周训练时长和刷题量是多少？我们在家如何配合？",
    ]

    return {
        "student": student_d,
        "luogu_uid": luogu_uid,
        "gesp_level": gesp_level,
        "gesp_score": gesp_score,
        "next_level": next_lv,
        "exemptions": exemptions,
        "can_exempt_cspj": can_exempt_cspj,
        "can_exempt_csps": can_exempt_csps,
        "gesp_gap": gesp_gap,
        "target": target,
        "timeline": timeline,
        "policy_events": policy_events[:8],
        "diff_dist": diff_dist,
        "last_exam": last_exam,
        "questions": questions,
    }


@app.route("/me/<luogu_uid>/parent-subscribe", methods=["GET", "POST"])
def parent_subscribe(luogu_uid: str):
    """v3.5.2 · 家长订阅版（真 AI 二次生成）

    GET 行为：
      - 学员已存在 + 已有 parent_subscribe.html → 渲染 AI 生成版
      - 学员已存在 + 还没生成 → 渲染"触发生成"页（POST 触发）
      - 学员未注册 → 404

    POST 行为：直接重定向到 /me/<uid>/start-parent-subscribe（用 form 提交也行）
    """
    # v3.5.2 传播期：商业化暂不开放
    if _HIDE_COMMERCE:
        return render_template_string(COMMERCE_PAUSED_HTML), 503
    student = _admin_students.get_student_by_uid(luogu_uid)
    if not student:
        return render_template_string(REGISTER_INVALID_HTML, message=f"UID {luogu_uid} 未注册"), 404

    # 找该学员最近一份 report 文件夹
    report_dir = _find_latest_report_dir(luogu_uid, student.get("real_name") or "")
    ps_html = (report_dir / "parent_subscribe.html") if report_dir else None
    ps_md = (report_dir / "parent_subscribe.md") if report_dir else None

    # 已生成 → 直接渲染（外层套一个家长友好壳）
    if ps_html and ps_html.exists():
        html_body = ps_html.read_text(encoding="utf-8", errors="replace")
        return render_template_string(
            PARENT_SUBSCRIBE_RESULT_HTML,
            student_name=student.get("real_name") or "选手",
            luogu_uid=luogu_uid,
            md_url=f"/reports/{report_dir.name}/parent_subscribe.md" if ps_md and ps_md.exists() else "",
            generated_at=ps_md.read_text(encoding="utf-8", errors="replace")[-200:] if ps_md and ps_md.exists() else "",
            ai_body=html_body,
        )

    # 还没生成 → 渲染触发生成页
    data = _build_parent_subscribe_data(student, luogu_uid)
    has_report = bool(report_dir and (report_dir / "report.md").exists())
    return render_template_string(
        PARENT_SUBSCRIBE_HTML,
        **data,
        has_report=has_report,
        report_dir_name=report_dir.name if report_dir else "",
    )


@app.route("/me/<luogu_uid>/start-parent-subscribe", methods=["POST", "GET"])
def start_parent_subscribe(luogu_uid: str):
    """v3.5.2 · 触发生成家长订阅版（异步）

    1. 找该 UID 最近一份 report 文件夹
    2. 创建 task，task_type='parent_subscribe'
    3. 后台线程调 generate_parent_subscribe()
    4. 写到 report_dir/parent_subscribe.md
    5. 渲染成 HTML 写到 report_dir/parent_subscribe.html
    6. 跳转到 /status/<task_id>
    """
    # v3.5.2 传播期：商业化暂不开放
    if _HIDE_COMMERCE:
        return render_template_string(COMMERCE_PAUSED_HTML), 503
    student = _admin_students.get_student_by_uid(luogu_uid)
    if not student:
        return render_template_string(REGISTER_INVALID_HTML, message=f"UID {luogu_uid} 未注册"), 404

    report_dir = _find_latest_report_dir(luogu_uid, student.get("real_name") or "")
    if not report_dir or not (report_dir / "report.md").exists():
        gesp_level = int(student.get("gesp_highest_passed") or 0)
        gesp_score = int(student.get("gesp_latest_score") or 0)
        return render_template_string(
            PARENT_SUBSCRIBE_HTML,
            student=student,
            luogu_uid=luogu_uid,
            has_report=False,
            report_dir_name="",
            error_msg="还没生成过基础报告，无法生成家长订阅版。请先在生成报告页跑一次。",
            gesp_level=gesp_level,
            gesp_score=gesp_score,
            next_level=gesp_level + 1 if gesp_level < 8 else 8,
            can_exempt_cspj=gesp_level >= 7 and gesp_score >= 80,
            can_exempt_csps=gesp_level >= 8 and gesp_score >= 80,
            gesp_gap=max(0, 60 - gesp_score) if gesp_level else 60,
            target="—",
            timeline={"conservative": "—", "aggressive": "—", "fallback": "—"},
            policy_events=[],
            diff_dist={},
            last_exam=None,
            questions=[],
        ), 400

    # 解析 API key（从 form / 环境）
    form = request.form.to_dict() if request.method == "POST" else {}
    api_key, api_key_source = resolve_openai_api_key(form)
    if not api_key:
        gesp_level = int(student.get("gesp_highest_passed") or 0)
        gesp_score = int(student.get("gesp_latest_score") or 0)
        return render_template_string(
            PARENT_SUBSCRIBE_HTML,
            student=student,
            luogu_uid=luogu_uid,
            has_report=True,
            report_dir_name=report_dir.name,
            error_msg="未配置 OpenAI API Key。请在表单填写，或在服务端设置环境变量 OPENAI_API_KEY / OPENAI_ADMIN_KEY。",
            gesp_level=gesp_level,
            gesp_score=gesp_score,
            next_level=gesp_level + 1 if gesp_level < 8 else 8,
            can_exempt_cspj=gesp_level >= 7 and gesp_score >= 80,
            can_exempt_csps=gesp_level >= 8 and gesp_score >= 80,
            gesp_gap=max(0, 60 - gesp_score) if gesp_level else 60,
            target="—",
            timeline={"conservative": "—", "aggressive": "—", "fallback": "—"},
            policy_events=[],
            diff_dist={},
            last_exam=None,
            questions=[],
        ), 400

    # 优先：表单 > OPENAI_MODEL_NAME（与主报告流程一致）> 兜底
    model_name = (
        (form.get("model_name") or "").strip()
        or os.environ.get("OPENAI_MODEL_NAME", "").strip()
        or os.environ.get("OPENAI_MODEL", "").strip()  # 兼容旧名
        or "gpt-4o-mini"
    )
    base_url = (
        (form.get("base_url") or "").strip()
        or os.environ.get("OPENAI_BASE_URL", "").strip()
        or None
    )

    task_id = str(uuid.uuid4())
    with TASKS_LOCK:
        insert_task(task_id, status="running", message="正在生成家长订阅版...")
        update_task(
            task_id,
            stage="生成家长订阅版",
            task_type="parent_subscribe",
            luogu_uid=luogu_uid,
            student_name=student.get("real_name") or "选手",
            ai_progress=2,
            ai_elapsed_seconds=0,
        )
    thread = threading.Thread(
        target=_run_parent_subscribe,
        args=(task_id, report_dir, api_key, api_key_source, base_url, model_name, luogu_uid),
        daemon=True,
    )
    register_active_generation_task(task_id, thread)
    thread.start()
    return redirect(url_for("status_page", task_id=task_id, luogu_uid=luogu_uid))


def _find_latest_report_dir(luogu_uid: str, student_name: str = "") -> "Path | None":
    """找该学员最近一份 report 文件夹（reports/<task_id>_<name>/）

    v3.5.2 · 三段式匹配（按优先级降序）：
      1. 侧车文件 `luogu_uid.txt` 精确匹配（避免同姓名/同前缀误命中）
      2. 目录名包含 `luogu_uid`（旧式命名兼容）
      3. 目录名以 `_sanitized_name` 结尾（同一人多 UID 兜底）
    """
    reports_root = Path(__file__).parent / "reports"
    if not reports_root.exists():
        return None

    safe_name = "".join(c for c in (student_name or "") if c.isalnum() or c in "_-").strip()
    target_uid = str(luogu_uid or "").strip()

    exact: list = []
    legacy: list = []
    by_name: list = []
    for d in reports_root.iterdir():
        if not d.is_dir():
            continue
        if not (d / "report.md").exists():
            continue
        # 1) 侧车文件精确匹配
        if target_uid:
            try:
                if (d / "luogu_uid.txt").read_text(encoding="utf-8", errors="replace").strip() == target_uid:
                    exact.append(d)
                    continue
            except Exception:
                pass
        # 2) 旧式：目录名包含 luogu_uid
        if target_uid and target_uid in d.name:
            legacy.append(d)
            continue
        # 3) 兜底：同姓名（多 UID 一人）
        if safe_name and d.name.endswith(f"_{safe_name}"):
            by_name.append(d)

    pool = exact or legacy or by_name
    if not pool:
        return None
    pool.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return pool[0]


def _run_parent_subscribe(
    task_id: str,
    report_dir: Path,
    api_key: str,
    api_key_source: str,
    base_url: str | None,
    model_name: str,
    luogu_uid: str,
) -> None:
    """后台线程：读 report.md → 调 generate_parent_subscribe → 渲染 HTML"""
    import time as _time
    from luogu_evaluator import generate_parent_subscribe
    started = _time.time()
    try:
        with TASKS_LOCK:
            update_task(task_id, message=f"正在读取基础报告...", ai_progress=10)
        report_md = (report_dir / "report.md").read_text(encoding="utf-8", errors="replace")
        export_data_path = report_dir / "export_data.json"
        export_data = {}
        if export_data_path.exists():
            try:
                export_data = json.loads(export_data_path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                export_data = {}

        with TASKS_LOCK:
            update_task(
                task_id,
                message=f"({api_key_source}) 正在调用 {model_name} 生成家长订阅版...",
                ai_progress=30,
            )

        ps_md = generate_parent_subscribe(
            report_md=report_md,
            export_data=export_data,
            api_key=api_key,
            base_url=base_url,
            model_name=model_name,
        )
        if not ps_md or not ps_md.strip():
            raise ValueError("AI 返回空内容")

        # 写 markdown
        ps_md_path = report_dir / "parent_subscribe.md"
        ps_md_path.write_text(ps_md, encoding="utf-8")
        with TASKS_LOCK:
            update_task(task_id, message="家长订阅版 Markdown 已写入，正在渲染 HTML...", ai_progress=85)

        # 渲染 HTML —— 后台线程需要手动 push app context
        from markdown import markdown as _md
        ps_html_body = _md(
            ps_md,
            extensions=["tables", "fenced_code", "sane_lists", "toc"],
            output_format="html5",
        )
        # 套一层家长友好外壳
        student_name = ""
        try:
            from_student = export_data.get("student_info", {}) or {}
            student_name = from_student.get("real_name") or ""
        except Exception:
            pass
        if not student_name:
            student_name = report_dir.name.split("_", 1)[-1] if "_" in report_dir.name else "选手"

        with app.app_context():
            ps_html_full = render_template_string(
                _PARENT_SUBSCRIBE_SHELL_HTML,
                student_name=student_name,
                luogu_uid=luogu_uid,
                generated_at=_time.strftime("%Y-%m-%d %H:%M"),
                ai_body=ps_html_body,
                report_dir_name=report_dir.name,
            )
        ps_html_path = report_dir / "parent_subscribe.html"
        ps_html_path.write_text(ps_html_full, encoding="utf-8")
        elapsed = int(_time.time() - started)
        with TASKS_LOCK:
            update_task(
                task_id,
                status="done",
                message=f"家长订阅版已生成（{elapsed}s）",
                ai_progress=100,
                ai_elapsed_seconds=elapsed,
                ps_html=f"/reports/{report_dir.name}/parent_subscribe.html",
                ps_md=f"/reports/{report_dir.name}/parent_subscribe.md",
            )
    except Exception as exc:
        elapsed = int(_time.time() - started)
        with TASKS_LOCK:
            update_task(
                task_id,
                status="error",
                message=f"生成失败：{type(exc).__name__}: {exc}",
                ai_elapsed_seconds=elapsed,
            )
        log = app.logger
        log.exception("[parent_subscribe][%s] failed", task_id)


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

        <!-- v3.5.3 学员 GESP/CSP/NOIP/NOI 自录入区 · 锚点 #awards -->
        <div class="bg-white rounded-2xl shadow p-5 mb-4" id="awards">
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

        <!-- v3.5.2 传播期 · 位置图分享入口（v3.6 头部已加分享图标按钮，底部保留简化版仅作 anchor / 详情） -->
        <div class="bg-gradient-to-r from-emerald-50 to-teal-50 border border-emerald-200 rounded-2xl shadow p-4 mb-4 text-center">
            <p class="text-sm text-gray-700 mb-2">
                📌 想分享给家长群 / 朋友圈？<strong>点击页面顶部的 📤 分享按钮</strong>即可生成一张 GESP 段位 + 9 月免初赛 + 关键赛事倒计时的位置图。
            </p>
            <a href="javascript:void(0)" onclick="document.getElementById('shareModal').classList.remove('hidden')"
               class="inline-block px-4 py-2 bg-emerald-600 text-white text-sm font-bold rounded-lg hover:bg-emerald-700">
                📤 立即生成位置图
            </a>
            <details class="text-xs text-gray-600 mt-3 text-left">
                <summary class="cursor-pointer hover:text-gray-800 font-medium">📐 图里有什么？</summary>
                <ul class="mt-2 list-disc list-inside space-y-1 pl-2">
                    <li>标题：信息学AI测评结果 + 学员 UID / 姓名</li>
                    <li>AI 定级 + 性格画像雷达图 + 高频算法标签图</li>
                    <li>GESP 段位图（1✦ 2★ 3□ ... 8□）</li>
                    <li>当前 GESP 等级 + 距免初赛差距</li>
                    <li>2026 关键赛事倒计时（最多 4 场）</li>
                    <li>底部二维码：扫码直达你的位置图</li>
                    <li>水印：AI 估算 · 不替代真考 · 最后更新日期</li>
                </ul>
            </details>
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
            {% if not commerce_hidden %}
            <div class="bg-white border border-amber-200 rounded-lg p-3 mt-2">
                <p class="text-xs text-gray-700 mb-2">💡 家长加 V 兑换 <code class="font-mono">PARENT-SUB-XXXX</code> 后，<strong>AI 讲题自动解锁</strong>。这是"家长为孩子买"的家庭订阅模式。</p>
                <a href="/redeem" class="inline-block text-xs px-3 py-1.5 bg-amber-500 text-white rounded hover:bg-amber-600">🎁 兑换家长订阅码</a>
                <a href="/" class="inline-block text-xs px-3 py-1.5 border border-gray-300 text-gray-700 rounded hover:bg-gray-50 ml-1">加 V 获取 →</a>
            </div>
            {% endif %}
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
    """家长端入口页：v3.5.2（输入 token 跳转）

    v3.5.2 · 智能识别两种码，避免家长/客服混淆：
      · 32+ 位长串 → guardian.notify_token（教练在学员详情页生成），跳家长中心
      · PS-/PJC-/IJC- 前缀短码 → activation_codes（订阅/冲刺营），引导去 /redeem 兑换
    """
    error = None
    notice = None
    token = ""
    if request.method == "POST":
        token = (request.form.get("token") or "").strip()
        # 1) 看起来像激活兑换码（PS-/PJC-/IJC- 前缀）→ 引导去 /redeem
        upper = token.upper()
        if upper.startswith(("PS-", "PJC-", "IJC-")):
            return redirect(url_for("redeem_code", code=upper))
        # 2) 格式太短 → 直接拒绝
        if not token or not token.replace("-", "").replace("_", "").isalnum() or len(token) < 8:
            error = "家长 token 无效（应为 32+ 位字母数字，由教练在学员详情页生成）"
        else:
            g = _admin_guardians.get_guardian_by_token(token)
            if not g:
                error = "家长 token 未找到，请向教练索取正确链接"
            else:
                return redirect(url_for("parent_panel_index", token=token))
    return render_template_string(
        PARENT_TOKEN_ENTRY_HTML,
        error=error,
        notice=notice,
        token=token,
        commerce_hidden=_HIDE_COMMERCE,
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

        <!-- 4 SKU 付费 CTA（v3.5.1 转化入口）· v3.5.2 传播期隐藏 -->
        {% if not commerce_hidden %}
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
        {% endif %}

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
            <p class="text-sm text-gray-500">输入教练给您的家长 token</p>
        </div>
        {% if error %}
        <div class="mb-4 px-3 py-2 bg-red-50 border border-red-200 text-red-700 rounded-lg text-sm">⚠️ {{ error }}</div>
        {% endif %}
        <form method="POST" class="space-y-3">
            <div>
                <label class="block text-sm font-medium text-gray-700 mb-1">家长 token</label>
                <input type="text" name="token" required minlength="8" maxlength="64"
                       value="{{ token or '' }}"
                       placeholder="如：AbCdEf123XyZ…(32+ 位长串)"
                       class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono focus:ring-2 focus:ring-amber-500 focus:border-amber-500">
                <p class="text-xs text-gray-400 mt-1">由教练 1v1 邀请分发 · 32+ 位字母数字（带 - _）</p>
            </div>
            <button type="submit" class="w-full bg-gradient-to-r from-amber-500 to-amber-600 text-white font-bold py-2.5 rounded-lg hover:from-amber-600 hover:to-amber-700 transition">
                进入家长面板
            </button>
        </form>

        <!-- 兑换码引导（v3.5.2 新增：避免家长误把 PS-/PJC-/IJC- 当 token 填） -->
        <div class="mt-4 p-3 bg-blue-50 border border-blue-200 rounded-lg text-xs text-blue-800">
            <div class="font-bold mb-1">🎟️ 拿到的是 PS-/PJC-/IJC- 开头的短码？</div>
            <p class="mb-2">那是<span class="font-bold">订阅兑换码</span>（家长订阅 / 冲刺营），不是家长 token。请去兑换页激活：</p>
            <a href="/redeem" class="inline-block px-3 py-1.5 bg-blue-500 text-white rounded hover:bg-blue-600 text-xs font-bold">→ 前往 /redeem 兑换激活</a>
        </div>

        <!-- 加 V 引导（v3.5.2 终态） -->
        <div class="mt-3 p-3 bg-amber-50 border border-amber-200 rounded-lg text-xs text-amber-800">
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
    # 读取 FLASK_DEBUG 环境变量（与 `flask run` 一致），默认 off
    import os as _os
    _dbg = _os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=5000, debug=_dbg)
