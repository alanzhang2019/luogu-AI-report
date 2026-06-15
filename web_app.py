import os
from markupsafe import Markup
import json
import uuid
import threading
import time
import hmac
from pathlib import Path
from datetime import datetime, date, timedelta, timezone

# v3.9.38 · 北京时间统一 helper（防御性：即使容器 TZ 没设置也能正确转 Beijing）
# 之前用 datetime.utcnow() + 8h hack，依赖容器 TZ=Asia/Shanghai 后改回 datetime.now()，
# 但保留 _NOW_BJ() 作为兜底（用户本地浏览器也可能因各种原因看到错误时区）
_BJ_TZ = timezone(timedelta(hours=8))
def _NOW_BJ():
    """获取当前北京时间（aware datetime，TZ=Asia/Shanghai，UTC+8）"""
    return datetime.now(_BJ_TZ)
from urllib.parse import urlsplit, urlunsplit
from openai import APIConnectionError, APITimeoutError, APIError, RateLimitError as OpenAIRateLimitError
try:
    from flask import Flask, render_template_string, request, redirect, url_for, send_from_directory, send_file, session, flash, Response, make_response
except ImportError:
    from flask import Flask, render_template_string, request, redirect, url_for, send_from_directory, send_file, session, flash, Response, make_response
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


# v3.9.6 · 单一权威版本号（git tag、UI 页脚、deploy 健康检查、API /api/version 都读这里）
# 规则：每次对外发布（commit + push + 云端部署）必须 bump 这里的字符串
APP_VERSION = "v3.9.17"
APP_VERSION_BUILD = "20260614_v3p9p17"  # 日期 + 版本号（tag-style，便于一眼定位）
APP_GIT_COMMIT = os.environ.get("LUOGU_GIT_COMMIT", "dev")[:7]

app = Flask(__name__)
app.config["APP_VERSION"] = APP_VERSION
app.config["APP_VERSION_BUILD"] = APP_VERSION_BUILD
app.config["APP_GIT_COMMIT"] = APP_GIT_COMMIT
app.secret_key = (
    os.environ.get("ADMIN_SESSION_SECRET")
    or os.environ.get("FLASK_SECRET_KEY")
    or "luogu-ai-report-admin-secret-change-me"
)
# v3.8 · 学员注册后会话保持 180 天（"记住我" · 避免重复输入 UID/姓名）
app.permanent_session_lifetime = timedelta(days=180)


# v3.9.6 · 版本探针（给云端部署的健康检查用，每次部署完必看）
# 用法：curl -fsS http://server/api/version  →  {"version":"v3.9.6", "build":"...", "git":"abc1234", "ts":"..."}
@app.route("/api/version")
def _api_version():
    import json as _json, datetime as _dt
    return _json.dumps({
        "version": APP_VERSION,
        "build": APP_VERSION_BUILD,
        "git": APP_GIT_COMMIT,
        "ts": _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }, ensure_ascii=False), 200, {"Content-Type": "application/json; charset=utf-8"}


# v3.9.5 · 模板全局变量（页脚 / 家长报告页头显示版本号）
@app.context_processor
def _inject_app_version():
    return {
        "APP_VERSION": APP_VERSION,
        "APP_VERSION_BUILD": APP_VERSION_BUILD,
        "APP_GIT_COMMIT": APP_GIT_COMMIT,
    }


# v3.8 · 中国省市二级联动数据（用于 /generate-form 表单 · 城市 + CSP 省份）
CHINA_REGIONS: list[dict] = [
    # 直辖市（自身即"城市"）
    {"code": "11", "name": "北京", "cities": ["北京"]},
    {"code": "12", "name": "天津", "cities": ["天津"]},
    {"code": "31", "name": "上海", "cities": ["上海"]},
    {"code": "50", "name": "重庆", "cities": ["重庆"]},
    # 省
    {"code": "13", "name": "河北", "cities": ["石家庄", "唐山", "秦皇岛", "保定", "邯郸", "廊坊", "沧州", "邢台", "张家口", "承德", "衡水"]},
    {"code": "14", "name": "山西", "cities": ["太原", "大同", "临汾", "运城", "晋中", "长治", "晋城", "吕梁", "忻州", "朔州", "阳泉"]},
    {"code": "15", "name": "内蒙古", "cities": ["呼和浩特", "包头", "鄂尔多斯", "赤峰", "通辽", "呼伦贝尔", "巴彦淖尔", "乌兰察布"]},
    {"code": "21", "name": "辽宁", "cities": ["沈阳", "大连", "鞍山", "抚顺", "本溪", "丹东", "锦州", "营口", "阜新", "辽阳", "盘锦", "铁岭", "朝阳", "葫芦岛"]},
    {"code": "22", "name": "吉林", "cities": ["长春", "吉林", "延边", "四平", "通化", "白城", "辽源", "白山", "松原"]},
    {"code": "23", "name": "黑龙江", "cities": ["哈尔滨", "大庆", "齐齐哈尔", "牡丹江", "佳木斯", "鸡西", "鹤岗", "双鸭山", "黑河", "伊春", "绥化"]},
    {"code": "32", "name": "江苏", "cities": ["南京", "苏州", "无锡", "常州", "南通", "徐州", "扬州", "盐城", "淮安", "连云港", "镇江", "泰州", "宿迁"]},
    {"code": "33", "name": "浙江", "cities": ["杭州", "宁波", "温州", "嘉兴", "绍兴", "金华", "台州", "湖州", "丽水", "衢州", "舟山"]},
    {"code": "34", "name": "安徽", "cities": ["合肥", "芜湖", "蚌埠", "阜阳", "淮南", "安庆", "滁州", "六安", "马鞍山", "宿州", "宣城", "铜陵", "淮北", "黄山", "池州"]},
    {"code": "35", "name": "福建", "cities": ["福州", "厦门", "泉州", "漳州", "莆田", "三明", "南平", "龙岩", "宁德"]},
    {"code": "36", "name": "江西", "cities": ["南昌", "九江", "赣州", "宜春", "吉安", "上饶", "抚州", "景德镇", "萍乡", "新余", "鹰潭"]},
    {"code": "37", "name": "山东", "cities": ["济南", "青岛", "烟台", "潍坊", "临沂", "淄博", "济宁", "泰安", "威海", "德州", "聊城", "滨州", "东营", "菏泽", "枣庄", "日照"]},
    {"code": "41", "name": "河南", "cities": ["郑州", "洛阳", "开封", "新乡", "信阳", "南阳", "安阳", "焦作", "平顶山", "许昌", "商丘", "周口", "驻马店", "鹤壁", "濮阳", "三门峡", "漯河", "济源"]},
    {"code": "42", "name": "湖北", "cities": ["武汉", "黄冈", "宜昌", "襄阳", "荆州", "十堰", "孝感", "黄石", "咸宁", "荆门", "鄂州", "随州", "恩施"]},
    {"code": "43", "name": "湖南", "cities": ["长沙", "衡阳", "株洲", "岳阳", "常德", "湘潭", "永州", "邵阳", "益阳", "郴州", "怀化", "娄底", "湘西", "张家界"]},
    {"code": "44", "name": "广东", "cities": ["广州", "深圳", "东莞", "佛山", "中山", "珠海", "惠州", "汕头", "湛江", "江门", "肇庆", "茂名", "揭阳", "梅州", "清远", "韶关", "阳江", "潮州", "汕尾", "河源", "云浮"]},
    {"code": "45", "name": "广西", "cities": ["南宁", "桂林", "柳州", "梧州", "北海", "贵港", "玉林", "百色", "钦州", "河池", "防城港", "来宾", "崇左"]},
    {"code": "46", "name": "海南", "cities": ["海口", "三亚", "三沙", "儋州", "五指山", "琼海", "文昌", "万宁", "东方"]},
    {"code": "51", "name": "四川", "cities": ["成都", "绵阳", "南充", "宜宾", "泸州", "德阳", "乐山", "达州", "自贡", "广安", "遂宁", "内江", "眉山", "广元", "雅安", "巴中", "资阳", "甘孜", "凉山", "阿坝"]},
    {"code": "52", "name": "贵州", "cities": ["贵阳", "遵义", "六盘水", "安顺", "毕节", "铜仁", "黔东南", "黔南", "黔西南"]},
    {"code": "53", "name": "云南", "cities": ["昆明", "大理", "丽江", "曲靖", "玉溪", "红河", "楚雄", "文山", "普洱", "昭通", "西双版纳", "保山", "临沧", "德宏", "迪庆", "怒江"]},
    {"code": "54", "name": "西藏", "cities": ["拉萨", "日喀则", "昌都", "林芝", "山南", "那曲", "阿里"]},
    {"code": "61", "name": "陕西", "cities": ["西安", "咸阳", "宝鸡", "延安", "汉中", "渭南", "榆林", "安康", "商洛", "铜川"]},
    {"code": "62", "name": "甘肃", "cities": ["兰州", "天水", "嘉峪关", "酒泉", "张掖", "武威", "白银", "平凉", "庆阳", "定西", "陇南", "临夏", "甘南"]},
    {"code": "63", "name": "青海", "cities": ["西宁", "海东", "海西", "海南", "海北", "玉树", "黄南", "果洛"]},
    {"code": "64", "name": "宁夏", "cities": ["银川", "石嘴山", "吴忠", "固原", "中卫"]},
    {"code": "65", "name": "新疆", "cities": ["乌鲁木齐", "喀什", "伊宁", "克拉玛依", "吐鲁番", "哈密", "阿克苏", "和田", "阿勒泰", "塔城", "昌吉", "博尔塔拉", "巴音郭楞", "克孜勒苏"]},
]


def _get_region_options() -> dict:
    """v3.8 · 生成前端省市二级联动 JSON

    返回：
        {
            "regions": [
                {"code": "11", "name": "北京", "cities": ["北京"]},
                ...
            ],
            "region_names": ["北京", "天津", "上海", ...]  # 省份列表
        }
    """
    regions = CHINA_REGIONS
    region_names = [r["name"] for r in regions]
    return {
        "regions": regions,
        "region_names": region_names,
    }


# 注入到 Jinja 模板全局（一次注册，所有 render_template_string 自动可用）
try:
    app.jinja_env.globals["region_options"] = _get_region_options()
except Exception as _je:
    app.logger.warning(f"region_options 注入失败: {_je}")


def _set_student_session(luogu_uid: str, student_id: int, real_name: str = "") -> None:
    """v3.8 · 注册/识别成功后写入学员会话（永久 cookie · 180 天）"""
    try:
        session.permanent = True
        session["student_uid"] = str(luogu_uid).strip()
        session["student_sid"] = int(student_id) if student_id else 0
        session["student_name"] = (real_name or "").strip()
        session["student_login_at"] = datetime.now().isoformat(timespec="seconds")
    except Exception as _e:
        app.logger.warning(f"_set_student_session failed: {_e}")


def _load_student_form_from_session() -> dict:
    """v3.8 · 从 session 读取最近一次登录学员，回填 GENERATE_FORM_HTML 的 form 字段"""
    uid = str(session.get("student_uid") or "").strip()
    try:
        stu = _admin_students.get_student_by_uid(uid)
        if not stu:
            return {}
        # 同步读取手机号（v3.5.2 注册时存到 guardians）
        phone = ""
        try:
            from task_store import _get_conn
            conn = _get_conn()
            try:
                row = conn.execute(
                    "SELECT g.phone FROM guardians g JOIN students s ON s.id = g.student_id "
                    "WHERE s.luogu_uid = ? ORDER BY g.id DESC LIMIT 1",
                    (uid,),
                ).fetchone()
                if row:
                    phone = dict(row).get("phone") or ""
            finally:
                conn.close()
        except Exception:
            pass
        # 同步读取 GESP 自录历史奖项（如有）
        gesp_level = ""
        gesp_score = ""
        gesp_year = ""
        gesp_cert = ""
        try:
            from admin_students import get_student_gesp_progress
            prog = get_student_gesp_progress(int(stu.get("id") or 0)) or {}
            # 取最高级别的最近一次
            best = None
            for ex in (prog.get("exams") or []):
                if not best or int(ex.get("level") or 0) > int(best.get("level") or 0):
                    best = ex
            if best:
                gesp_level = str(best.get("level") or "")
                gesp_score = str(best.get("score") or "")
                gesp_year = str(best.get("award_year") or best.get("exam_year") or "")
                gesp_cert = best.get("certificate_no") or ""
        except Exception:
            pass
        return {
            "uid": uid,
            "real_name": (stu.get("real_name") or "").strip(),
            "city": (stu.get("city") or "").strip(),
            "province": (stu.get("province") or "").strip(),  # v3.8 · 省份回填
            "grade": (stu.get("grade") or "").strip(),
            "gender": (stu.get("gender") or "").strip(),
            "school": (stu.get("school") or "").strip(),
            "birth_date": (stu.get("birth_date") or "").strip(),
            "phone": phone,
            "gesp_level": gesp_level,
            "gesp_score": gesp_score,
            "gesp_year": gesp_year,
            "gesp_certificate_no": gesp_cert,
            # 洛谷 cookies 和 OpenAI 配置不持久化（安全性）
            "client_id": "",
            "c3vk": "",
            "api_key": "",
            "base_url": "",
            "model_name": "",
            "_from_session": True,  # 标记：来自 session，前端可显示"已登录"
            "_student_name": (stu.get("real_name") or "").strip() or f"UID {uid}",
        }
    except Exception as _e:
        app.logger.warning(f"_load_student_form_from_session failed: {_e}")
        return {}


# v3.7.1 · 全站统一皮肤（与首页 INDEX_HTML 风格一致：emerald/teal 主色 + 渐变背景）
# 所有页面在 <head> 内插入 {{ app_skin_head() }} 即可自动获得：
#   - app-body  body 渐变 + 统一字体
#   - app-card  标准卡片（白底 / 16px 圆角 / 阴影）
#   - app-title / app-subtitle / app-tag  标题规范
#   - app-btn-primary / app-btn-secondary / app-btn-amber  按钮规范
#   - app-box-{yellow|blue|green|red}  状态条
_APP_SKIN_CSS = r"""
<style id="app-skin">
.app-body{background:linear-gradient(135deg,#f0fdf4 0%,#ecfeff 100%);min-height:100vh;
  font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,"Noto Sans","PingFang SC","Microsoft YaHei",sans-serif;
  color:#1f2937;}
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
.app-input{margin-top:6px;display:block;width:100%;border-radius:10px;border:1px solid #d1d5db;padding:10px 12px;box-shadow:0 1px 2px rgba(0,0,0,.04);background:#fff;}
.app-input:focus{outline:none;border-color:#10b981;box-shadow:0 0 0 3px rgba(16,185,129,.2);}
.app-btn{display:inline-flex;align-items:center;justify-content:center;width:100%;border-radius:10px;padding:10px 14px;font-weight:800;transition:all .15s ease;cursor:pointer;border:0;}
.app-btn-primary{background:linear-gradient(135deg,#059669 0%,#0d9488 100%);color:#fff;}
.app-btn-primary:hover{background:linear-gradient(135deg,#047857 0%,#0f766e 100%);transform:translateY(-1px);box-shadow:0 4px 12px rgba(5,150,105,.3);}
.app-btn-secondary{background:#fff;color:#047857;border:1px solid #6ee7b7;}
.app-btn-secondary:hover{background:#ecfdf5;}
.app-btn-amber{background:linear-gradient(135deg,#f59e0b 0%,#d97706 100%);color:#fff;}
.app-btn-amber:hover{background:linear-gradient(135deg,#d97706 0%,#b45309 100%);transform:translateY(-1px);box-shadow:0 4px 12px rgba(245,158,11,.3);}
.app-btn:disabled{opacity:.5;cursor:not-allowed;}
.app-link{color:#047857;text-decoration:none;}
.app-link:hover{text-decoration:underline;}
/* 状态 pill：完成/失败/进行中 统一改为 emerald/amber/rose 系 */
.app-pill{display:inline-block;padding:3px 10px;border-radius:9999px;font-size:12px;font-weight:600;}
.app-pill-done{background:#d1fae5;color:#065f46;}
.app-pill-error{background:#fee2e2;color:#991b1b;}
.app-pill-running{background:#fef3c7;color:#92400e;}
.app-pill-muted{background:#f3f4f6;color:#374151;}
/* 进度条 */
.app-progress{width:100%;background:#e5e7eb;border-radius:9999px;height:10px;overflow:hidden;}
.app-progress > .app-progress-fill{height:100%;background:linear-gradient(90deg,#10b981,#0d9488);transition:width .4s ease;}
/* 表格 */
.app-table{width:100%;border-collapse:collapse;font-size:14px;}
.app-table thead{background:#ecfdf5;color:#065f46;}
.app-table th{padding:10px 14px;text-align:left;font-weight:700;border-bottom:1px solid #d1fae5;}
.app-table td{padding:10px 14px;border-bottom:1px solid #f3f4f6;color:#374151;}
.app-table tr:hover td{background:#f9fafb;}
</style>
"""


def _app_skin_head() -> str:
    """v3.7.1 · 全站统一皮肤（用于每个页面的 <head> 尾部插入）。

    设计目标：所有页面与 INDEX_HTML 风格一致（emerald/teal 主色 + 浅绿渐变背景）。
    """
    return Markup(_APP_SKIN_CSS)


# 注册到 Jinja2 全局，所有 render_template_string 调用都能直接用 {{ app_skin_head() }}
app.jinja_env.globals["app_skin_head"] = _app_skin_head


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
    {{ app_skin_head() }}
</head>
<body class="app-body flex items-center justify-center p-4">
    <div class="app-card max-w-2xl w-full">
        <div class="text-center mb-4">
            <div class="app-pill app-pill-done mb-2">v3.5.2 · 传播期模式</div>
            <h1 class="app-title">🌱 先把基础用户跑起来</h1>
            <p class="app-subtitle">商业化（家长订阅 / 冲刺营）将在 <strong>100+ 真实学员</strong>之后再揭幕。</p>
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


# ========== v3.8 · 标签 & 作业记录 全局磁盘缓存（重试秒过） ==========
# 标签是洛谷全局数据，所有用户共享一份 → _ROOT/.source_cache/_tag_maps.json
# 作业记录是按用户隔离 → _ROOT/.source_cache/<uid>/_practice.json
# 这两个数据每用户每小时变动很小，但当前实现每次跑报告都会重拉，
# 加缓存后 "返回重试" 可以秒级跳过洛谷 API，只花 AI 生成时间。
_TAG_MAPS_CACHE_FILE = _ROOT / ".source_cache" / "_tag_maps.json"
_PRACTICE_CACHE_TTL_SECONDS = 6 * 3600  # 作业记录 6h 过期（标签基本不变，可视为永久）


def _load_cached_tag_maps() -> tuple[dict | None, dict | None, str]:
    """读取标签缓存（全局），返回 (tag_by_id, type_by_id, cached_at) 或 (None, None, '')

    v3.8 修复：JSON 加载后 dict 的 key 都是 str，但 _summarize 等下游函数用 int 查询，
    会导致 algorithm_tag_counter 永远为 0。这里加载时把 key 强制转回 int。
    """
    if not _TAG_MAPS_CACHE_FILE.exists():
        return None, None, ""
    try:
        payload = json.loads(_TAG_MAPS_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None, None, ""
    if not isinstance(payload, dict):
        return None, None, ""
    raw_tag_by_id = payload.get("tag_by_id")
    raw_type_by_id = payload.get("type_by_id")
    cached_at = str(payload.get("_cached_at", "") or "")
    if not isinstance(raw_tag_by_id, dict) or not isinstance(raw_type_by_id, dict):
        return None, None, ""
    # 把 key 转回 int（_build_tag_maps 原始输出用 int key）
    tag_by_id: dict[int, dict] = {}
    for k, v in raw_tag_by_id.items():
        try:
            tag_by_id[int(k)] = v
        except (TypeError, ValueError):
            continue
    type_by_id: dict[int, dict] = {}
    for k, v in raw_type_by_id.items():
        try:
            type_by_id[int(k)] = v
        except (TypeError, ValueError):
            continue
    return tag_by_id, type_by_id, cached_at


def _save_cached_tag_maps(tag_by_id: dict, type_by_id: dict) -> None:
    """保存标签缓存（全局）"""
    try:
        _TAG_MAPS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "tag_by_id": tag_by_id,
            "type_by_id": type_by_id,
            "_cached_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _TAG_MAPS_CACHE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except Exception as _e:
        app.logger.warning(f"v3.8 保存 tag_maps 缓存失败: {_e}")


def _load_cached_practice(uid: int | str) -> dict | None:
    """读取作业记录缓存（按 uid，6h TTL）"""
    cache_file = _source_cache_dir(uid) / "_practice.json"
    if not cache_file.exists():
        return None
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    cached_at_str = str(payload.get("_cached_at", "") or "")
    try:
        cached_at = datetime.strptime(cached_at_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None
    if (datetime.now() - cached_at).total_seconds() > _PRACTICE_CACHE_TTL_SECONDS:
        return None  # 过期
    practice = payload.get("practice")
    if not isinstance(practice, dict):
        return None
    return practice


def _save_cached_practice(uid: int | str, practice_obj) -> None:
    """保存作业记录缓存（按 uid）

    Args:
        practice_obj: luogu.get_user_practice(uid) 的返回值（有 .data 属性）
    """
    try:
        data = getattr(practice_obj, "data", None)
        if data is None:
            return
        cache_file = _source_cache_dir(uid) / "_practice.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "practice": data if isinstance(data, dict) else {},
            "_cached_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        cache_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except Exception as _e:
        app.logger.warning(f"v3.8 保存 practice 缓存失败: {_e}")


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

    # v3.9 · 预加载 students 表（name → uid 映射），用于把孤儿报告反向关联到 UID
    # 原因：旧 reports 目录名形如 `1df7354a_赵永浩` / `25c937b3_付胤睿`，
    #       这些 task_id 在 tasks 表已无记录（v3.5.x 重构时清掉过），
    #       但赵永浩/付胤睿在 students 表有完整档案（含 UID），
    #       之前的"无 UID（孤儿报告）"显示完全错。
    from task_store import _get_conn
    name_to_uid: dict[str, str] = {}
    try:
        conn = _get_conn()
        try:
            for r in conn.execute("SELECT luogu_uid, real_name FROM students WHERE real_name IS NOT NULL AND real_name != ''"):
                uid = str(r["luogu_uid"] or "").strip()
                nm = str(r["real_name"] or "").strip()
                if uid and nm:
                    name_to_uid[nm] = uid
        finally:
            conn.close()
    except Exception:
        pass

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

        # v3.9 · 反向查 UID：按目录名/JSON 名匹配 students 表
        orphan_uid = name_to_uid.get(name) or name_to_uid.get(name_from_dir) or ""

        existing_files = [path for path in (html_path, pdf_path, md_path, export_json_path) if path.exists()]
        latest_time = max((path.stat().st_mtime for path in existing_files), default=report_dir.stat().st_mtime)
        # v3.9.38 · 显式转北京时间（之前用 datetime.fromtimestamp() 是 UTC 偏 8h）
        display_time = eval_time or datetime.fromtimestamp(latest_time, tz=_BJ_TZ).strftime("%Y-%m-%d %H:%M")

        discovered.append({
            "id": folder_name,
            "luogu_uid": orphan_uid,  # v3.9 · 之前永远空，改为反查 students
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
            "rebuild_message": "该报告目录未入库，仅支持查看与下载。" if not orphan_uid else "该报告目录未入库（task_id 已丢失），但已通过学员档案反查到 UID。",
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
        # v3.9.15 · 启发式校验：表单填的"非 sk- 开头"的串
        # （如登录密码、卡密、随手字符串）会被七牛云/所有 OpenAI 兼容服务
        # 一律 401 拒掉。之前 6 次重试失败就是这个原因。
        # 启发式判断：合法 API Key 通常以 sk- / sk-.../ 等前缀开头
        # 且长度 >= 20。如果表单填的不符合，自动忽略、走 .env。
        # 启发式不会把"短 sk-test"误杀（sk- 后接任意字符就放过）。
        looks_like_key = from_form.startswith(("sk-", "key-", "API-", "Bearer ")) or len(from_form) >= 32
        if not looks_like_key:
            app.logger.warning(
                f"[v3.9.15] 表单 api_key 不像合法 Key（前缀/长度不对）: {from_form[:6]}*** "
                f"（长度 {len(from_form)}），自动忽略、改用服务端 .env",
            )
            from_form = ""
        else:
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
    <title>信竞 AI 报告 · 选手成长平台 · v3.6</title>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700;12..96,800&family=DM+Sans:opsz,wght@9..40,400;9..40,500;9..40,700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        :root{
            --bg-0:#06080F;
            --bg-1:#0B1024;
            --bg-2:#11173A;
            --ink:#E5E7EB;
            --ink-2:#94A3B8;
            --ink-3:#64748B;
            --line:rgba(148,163,184,.16);
            --line-2:rgba(148,163,184,.28);
            --accent:#00FFB3;       /* AI 信号绿 */
            --accent-2:#7B61FF;     /* 紫色辉光 */
            --amber:#FFB627;        /* 琥珀高亮 */
            --rose:#FF6B9D;         /* 家长版粉 */
        }
        *{box-sizing:border-box}
        html,body{background:var(--bg-0);color:var(--ink);}
        body{
            font-family:"DM Sans",ui-sans-serif,system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
            font-feature-settings:"ss01","cv01";
            min-height:100vh;
            overflow-x:hidden;
        }
        .font-display{font-family:"Bricolage Grotesque",serif;font-optical-sizing:auto;letter-spacing:-.02em;}
        .font-mono{font-family:"JetBrains Mono",ui-monospace,"SF Mono",Menlo,Consolas,monospace;}

        /* ===== 背景：深空 + 网格 + 极光 ===== */
        .bg-space{
            position:fixed;inset:0;z-index:-2;
            background:
                radial-gradient(60% 50% at 15% 12%, rgba(123,97,255,.22) 0%, transparent 60%),
                radial-gradient(50% 40% at 90% 8%, rgba(0,255,179,.18) 0%, transparent 60%),
                radial-gradient(60% 60% at 80% 95%, rgba(255,107,157,.16) 0%, transparent 60%),
                linear-gradient(180deg, #06080F 0%, #0B1024 50%, #06080F 100%);
        }
        .bg-grid{
            position:fixed;inset:0;z-index:-1;pointer-events:none;
            background-image:
                radial-gradient(rgba(148,163,184,.18) 1px, transparent 1px);
            background-size: 28px 28px;
            background-position: -1px -1px;
            mask-image: radial-gradient(ellipse 80% 60% at 50% 30%, #000 30%, transparent 75%);
            -webkit-mask-image: radial-gradient(ellipse 80% 60% at 50% 30%, #000 30%, transparent 75%);
        }
        .bg-scan{
            position:fixed;inset:0;z-index:-1;pointer-events:none;opacity:.4;
            background: repeating-linear-gradient(180deg, transparent 0, transparent 3px, rgba(0,255,179,.012) 3px, rgba(0,255,179,.012) 4px);
        }

        /* ===== 顶栏状态条 ===== */
        .statusbar{
            border-bottom:1px solid var(--line);
            background:linear-gradient(180deg, rgba(11,16,36,.85), rgba(6,8,15,.65));
            backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
            font-family:"JetBrains Mono",monospace;font-size:11.5px;
        }
        .pulse-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 0 rgba(0,255,179,.6);animation:pulse 1.8s infinite;}
        @keyframes pulse{
            0%{box-shadow:0 0 0 0 rgba(0,255,179,.55)}
            70%{box-shadow:0 0 0 9px rgba(0,255,179,0)}
            100%{box-shadow:0 0 0 0 rgba(0,255,179,0)}
        }

        /* ===== 标题光标 ===== */
        .caret{display:inline-block;width:.55ch;height:1em;background:var(--accent);margin-left:.15em;vertical-align:-.12em;animation:blink 1.05s steps(1) infinite;}
        @keyframes blink{50%{opacity:0}}

        /* ===== 玻璃卡片 ===== */
        .glass{
            background:linear-gradient(180deg, rgba(17,23,42,.65), rgba(11,16,36,.55));
            border:1px solid var(--line);
            border-radius:18px;
            backdrop-filter:blur(14px) saturate(140%);
            -webkit-backdrop-filter:blur(14px) saturate(140%);
            box-shadow:0 30px 60px -20px rgba(0,0,0,.5), inset 0 1px 0 rgba(255,255,255,.04);
        }
        .glass-bright{
            background:linear-gradient(180deg, rgba(0,255,179,.06), rgba(11,16,36,.4) 60%);
            border:1px solid rgba(0,255,179,.28);
        }
        .glass-pink{
            background:linear-gradient(180deg, rgba(255,107,157,.07), rgba(11,16,36,.4) 60%);
            border:1px solid rgba(255,107,157,.26);
        }
        .glass-violet{
            background:linear-gradient(180deg, rgba(123,97,255,.08), rgba(11,16,36,.4) 60%);
            border:1px solid rgba(123,97,255,.26);
        }

        /* ===== 主 CTA 按钮 ===== */
        .btn-primary{
            position:relative;display:inline-flex;align-items:center;justify-content:center;gap:.55em;
            width:100%;
            padding:14px 20px;border-radius:14px;
            font-family:"Bricolage Grotesque",serif;font-weight:700;font-size:16px;letter-spacing:.01em;
            color:#02110B;cursor:pointer;border:0;
            background:linear-gradient(135deg,#00FFB3 0%,#7B61FF 100%);
            box-shadow:0 10px 30px -10px rgba(0,255,179,.5),0 6px 20px -8px rgba(123,97,255,.45);
            transition:transform .15s ease,box-shadow .2s ease,filter .15s ease;
            overflow:hidden;
        }
        .btn-primary::after{
            content:"";position:absolute;inset:0;
            background:linear-gradient(120deg,transparent 30%,rgba(255,255,255,.35) 50%,transparent 70%);
            transform:translateX(-120%);transition:transform .9s ease;
        }
        .btn-primary:hover{transform:translateY(-1px);box-shadow:0 18px 40px -10px rgba(0,255,179,.6),0 10px 25px -8px rgba(123,97,255,.55);filter:brightness(1.05);}
        .btn-primary:hover::after{transform:translateX(120%);}
        .btn-primary:active{transform:translateY(0)}
        .btn-secondary{
            display:inline-flex;align-items:center;justify-content:center;gap:.5em;
            padding:11px 16px;border-radius:12px;
            background:rgba(255,255,255,.04);
            color:var(--ink);
            border:1px solid var(--line-2);
            font-weight:600;font-size:13.5px;
            transition:all .15s ease;cursor:pointer;
        }
        .btn-secondary:hover{background:rgba(0,255,179,.08);border-color:rgba(0,255,179,.4);color:var(--accent);}

        /* ===== 特性卡片 ===== */
        .feat{position:relative;padding:22px 20px;border-radius:16px;background:linear-gradient(180deg,rgba(17,23,42,.6),rgba(11,16,36,.4));border:1px solid var(--line);transition:all .25s ease;overflow:hidden;}
        .feat:hover{transform:translateY(-3px);border-color:rgba(0,255,179,.4);box-shadow:0 18px 40px -18px rgba(0,255,179,.35);}
        .feat::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:linear-gradient(180deg,var(--accent),transparent);}
        .feat.pink::before{background:linear-gradient(180deg,var(--rose),transparent);}
        .feat.violet::before{background:linear-gradient(180deg,var(--accent-2),transparent);}
        .feat .num{font-family:"JetBrains Mono",monospace;font-size:11px;color:var(--ink-3);letter-spacing:.18em;}
        .feat .ic{font-size:26px;line-height:1;margin:6px 0 4px;display:inline-block;}
        .feat h3{font-family:"Bricolage Grotesque",serif;font-weight:700;font-size:16px;color:#fff;margin:0 0 6px;}
        .feat p{font-size:12.5px;color:var(--ink-2);line-height:1.55;margin:0;}

        /* ===== 滚动代码日志带 ===== */
        .ticker{
            position:relative;overflow:hidden;
            border-top:1px solid var(--line);border-bottom:1px solid var(--line);
            background:linear-gradient(180deg, rgba(11,16,36,.6), rgba(6,8,15,.4));
            font-family:"JetBrains Mono",monospace;font-size:11.5px;
        }
        .ticker::before,.ticker::after{content:"";position:absolute;top:0;bottom:0;width:60px;z-index:2;pointer-events:none;}
        .ticker::before{left:0;background:linear-gradient(90deg,var(--bg-0),transparent);}
        .ticker::after{right:0;background:linear-gradient(270deg,var(--bg-0),transparent);}
        .ticker-track{display:inline-flex;gap:42px;padding:10px 0;white-space:nowrap;animation:tick 48s linear infinite;}
        /* v3.8 · 强基 39 校：深色 glass 风格（与页面 hero/cards 统一） */
        .ticker-schools .ticker-track{display:inline-flex;gap:14px;padding:12px 18px;background:transparent;white-space:nowrap;animation:tick 90s linear infinite;}
        .school-chip{display:inline-flex;align-items:center;gap:8px;padding:6px 14px 6px 10px;border-radius:999px;background:linear-gradient(180deg,rgba(17,23,42,.72),rgba(11,16,36,.62));backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);box-shadow:inset 0 1px 0 rgba(255,255,255,.04),0 4px 12px rgba(0,0,0,.25);font-size:13.5px;font-weight:600;color:var(--ink);border:1px solid rgba(0,255,179,.22);transition:transform .25s ease,box-shadow .25s ease,border-color .25s ease,background .25s ease;}
        .school-chip:hover{transform:translateY(-2px);border-color:rgba(0,255,179,.55);box-shadow:inset 0 1px 0 rgba(255,255,255,.08),0 8px 20px rgba(0,255,179,.22);background:linear-gradient(180deg,rgba(17,23,42,.88),rgba(11,16,36,.78));}
        .school-emoji{font-size:15px;line-height:1;filter:saturate(1.1);}
        .school-img{width:24px;height:24px;object-fit:contain;flex-shrink:0;background:#fff;border-radius:4px;padding:2px;filter:drop-shadow(0 1px 1px rgba(0,0,0,.2));}
        /* v3.9 · PNG 校徽缺失时的默认占位（彩色圆圈 + 校名拼音缩写） */
        .school-badge{display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;border-radius:50%;flex-shrink:0;box-shadow:inset 0 0 0 1px rgba(255,255,255,.18),0 1px 2px rgba(0,0,0,.25);color:#fff;font-family:"JetBrains Mono","PingFang SC",ui-sans-serif,system-ui,sans-serif;font-weight:800;letter-spacing:-.02em;line-height:1;}
        .school-badge-text{font-size:7.5px;}
        .school-badge-text:not(:only-child){font-size:6.5px;}
        .school-name{font-family:'Noto Sans SC',ui-sans-serif,system-ui,sans-serif;letter-spacing:.02em;color:var(--ink);}
        .ticker-schools:hover .ticker-track{animation-play-state:paused;}
        @keyframes tick{from{transform:translateX(0)}to{transform:translateX(-50%)}}
        .tk-dot{color:var(--accent);}
        .tk-key{color:var(--amber);}
        .tk-num{color:var(--accent-2);}
        .tk-mute{color:var(--ink-3);}

        /* ===== 输入框 ===== */
        .field{
            width:100%;padding:11px 14px;border-radius:11px;
            background:rgba(6,8,15,.6);
            border:1px solid var(--line-2);
            color:var(--ink);font-family:"JetBrains Mono",monospace;font-size:13.5px;letter-spacing:.05em;
            transition:all .15s ease;
        }
        .field::placeholder{color:var(--ink-3);letter-spacing:0;}
        .field:focus{outline:none;border-color:rgba(0,255,179,.55);box-shadow:0 0 0 4px rgba(0,255,179,.12);background:rgba(6,8,15,.85);}

        /* ===== flash 提示 ===== */
        .flash{border-radius:12px;padding:10px 14px;border:1px solid var(--line);font-size:13px;display:flex;align-items:center;gap:8px;}
        .flash-yellow{background:rgba(255,182,39,.08);border-color:rgba(255,182,39,.3);color:#FCD34D;}
        .flash-red{background:rgba(255,107,157,.08);border-color:rgba(255,107,157,.3);color:#FCA5C0;}
        .flash-green{background:rgba(0,255,179,.08);border-color:rgba(0,255,179,.3);color:#6EE7B7;}
        .flash-blue{background:rgba(123,97,255,.08);border-color:rgba(123,97,255,.3);color:#C4B5FD;}

        /* ===== 客服卡 ===== */
        .contact-card{background:linear-gradient(180deg,rgba(17,23,42,.55),rgba(11,16,36,.4));border:1px solid var(--line);border-radius:14px;padding:16px;}
        .contact-card.amber{border-color:rgba(255,182,39,.25);}
        .contact-card.gray{border-color:var(--line-2);}

        /* ===== 浮入动画 ===== */
        .rise{opacity:0;transform:translateY(8px);animation:rise .7s ease forwards;}
        .rise.d1{animation-delay:.05s}
        .rise.d2{animation-delay:.12s}
        .rise.d3{animation-delay:.2s}
        .rise.d4{animation-delay:.28s}
        .rise.d5{animation-delay:.36s}
        .rise.d6{animation-delay:.44s}
        @keyframes rise{to{opacity:1;transform:translateY(0)}}

        /* ===== 主品牌 hero ===== */
        .hero-title{
            font-family:"Bricolage Grotesque",serif;
            font-weight:800;
            font-size:clamp(34px, 6vw, 60px);
            line-height:1.02;
            letter-spacing:-.035em;
            background:linear-gradient(180deg,#fff 0%,#94A3B8 130%);
            -webkit-background-clip:text;background-clip:text;color:transparent;
        }
        .hero-title .acc{background:linear-gradient(135deg,#00FFB3 0%,#7B61FF 60%,#FFB627 120%);-webkit-background-clip:text;background-clip:text;color:transparent;}
        .hero-sub{font-family:"JetBrains Mono",monospace;font-size:12.5px;color:var(--ink-2);letter-spacing:.04em;}
        .tag-chip{display:inline-flex;align-items:center;gap:6px;padding:5px 10px;border-radius:999px;background:rgba(0,255,179,.08);border:1px solid rgba(0,255,179,.28);color:#6EE7B7;font-family:"JetBrains Mono",monospace;font-size:11px;letter-spacing:.08em;}

        /* v3.8 · 首页主视觉下的 QQ 群号醒目条 */
        .qq-banner{
            display:inline-flex;align-items:center;gap:14px;margin:14px auto 0;
            padding:12px 16px 12px 14px;border-radius:14px;
            background:linear-gradient(135deg,rgba(0,255,179,.10) 0%,rgba(123,97,255,.10) 100%);
            border:1px solid rgba(0,255,179,.32);
            box-shadow:0 6px 20px -8px rgba(0,255,179,.28),inset 0 1px 0 rgba(255,255,255,.04);
            backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);
            max-width:100%;
        }
        .qq-banner:hover{border-color:rgba(0,255,179,.55);box-shadow:0 8px 24px -8px rgba(0,255,179,.42),inset 0 1px 0 rgba(255,255,255,.06);}
        .qq-banner-icon{display:inline-flex;align-items:center;justify-content:center;width:36px;height:36px;border-radius:10px;background:rgba(0,255,179,.12);color:var(--accent);flex-shrink:0;}
        .qq-banner-text{display:inline-flex;flex-direction:column;align-items:flex-start;line-height:1.2;gap:2px;}
        .qq-banner-label{font-family:"JetBrains Mono",monospace;font-size:10.5px;letter-spacing:.12em;color:var(--ink-2);text-transform:uppercase;}
        .qq-banner-num{font-family:"JetBrains Mono",monospace;font-size:18px;font-weight:800;letter-spacing:.05em;color:var(--accent);text-shadow:0 0 12px rgba(0,255,179,.35);}
        .qq-banner-copy{display:inline-flex;align-items:center;gap:6px;padding:7px 12px;border-radius:8px;background:rgba(0,255,179,.10);border:1px solid rgba(0,255,179,.4);color:var(--accent);font-family:"JetBrains Mono",monospace;font-size:11px;font-weight:700;letter-spacing:.06em;cursor:pointer;transition:all .15s ease;flex-shrink:0;}
        .qq-banner-copy:hover{background:rgba(0,255,179,.22);border-color:var(--accent);transform:translateY(-1px);box-shadow:0 4px 10px -2px rgba(0,255,179,.4);}
        .qq-banner-copy:active{transform:translateY(0);}
        @media (max-width:480px){
            .qq-banner{padding:10px 12px;gap:10px;}
            .qq-banner-icon{width:32px;height:32px;}
            .qq-banner-num{font-size:16px;}
        }

        /* 适配小屏 */
        @media (max-width:640px){
            .hero-title{font-size:38px}
        }
    </style>
</head>
<body>
<div class="bg-space"></div>
<div class="bg-grid"></div>
<div class="bg-scan"></div>

<!-- 顶部状态条 -->
<div class="statusbar">
    <div class="max-w-6xl mx-auto px-4 py-2 flex items-center justify-between gap-3 text-[var(--ink-2)]">
        <div class="flex items-center gap-3">
            <span class="pulse-dot"></span>
            <span class="text-[var(--accent)]">SYS</span><span class="tk-mute">::</span><span>SIGNAL_ACQUIRED</span>
            <span class="tk-mute hidden sm:inline">/</span>
            <span class="hidden sm:inline">LUOGU_REPORT_PIPELINE</span>
        </div>
        <div class="flex items-center gap-3">
            <span class="hidden md:inline">v3.6.0</span>
            <span id="statusbarClock" class="text-[var(--ink-2)]">--:--:--</span>
            <span class="text-[var(--ink-3)] hidden sm:inline">Asia/Shanghai</span>
        </div>
    </div>
</div>

<main class="max-w-4xl mx-auto px-4 pt-8 pb-12 space-y-5">

    {# v3.9.6 · 已登录学员横幅：避免每次重新输入 UID #}
    {{ logged_in_banner|safe }}

    <!-- flash 消息 -->
    {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
    <div class="space-y-2 rise d1">
        {% for category, message in messages %}
        <div class="flash {% if category == 'warning' %}flash-yellow{% elif category == 'error' %}flash-red{% elif category == 'success' %}flash-green{% else %}flash-blue{% endif %}">
            <span class="font-mono text-[10px] opacity-70">[{{ category|upper }}]</span>
            <span>{{ message }}</span>
        </div>
        {% endfor %}
    </div>
    {% endif %}
    {% endwith %}

    <!-- 品牌主视觉 -->
    <section class="text-center space-y-5 rise d2">
        <div class="flex items-center justify-center gap-2">
            <span class="tag-chip">
                <span class="pulse-dot" style="width:5px;height:5px"></span>
                AI · LUOGU · v3.6
            </span>
        </div>
        <h1 class="hero-title">
            信息学 AI 测评<br>
            <span class="acc">一站式选手成长平台</span>
        </h1>
        <p class="hero-sub max-w-xl mx-auto">
            &gt; 基于洛谷做题数据 · 多维算法画像 · 一键生成可分享测评报告
            <span class="caret"></span>
        </p>
        <!-- v3.8 · 醒目的 QQ 交流群入口（替代原底栏的 build/QQ 提示） -->
        <div class="qq-banner rise d3" role="group" aria-label="QQ 交流群">
            <span class="qq-banner-icon" aria-hidden="true">
                {# v3.9.7 · QQ 企鹅图标（inline SVG，避免外部资源错误） #}
                <svg viewBox="0 0 64 64" width="32" height="32" xmlns="http://www.w3.org/2000/svg" aria-label="QQ 企鹅">
                    <!-- 身体（白肚） -->
                    <ellipse cx="32" cy="44" rx="20" ry="18" fill="#FFFFFF" stroke="#0A0A0A" stroke-width="1.5"/>
                    <!-- 头部（黑） -->
                    <ellipse cx="32" cy="24" rx="16" ry="14" fill="#0A0A0A"/>
                    <!-- 眼睛（白色眼白） -->
                    <circle cx="26" cy="22" r="4.5" fill="#FFFFFF"/>
                    <circle cx="38" cy="22" r="4.5" fill="#FFFFFF"/>
                    <!-- 瞳孔（黑） -->
                    <circle cx="27" cy="23" r="2" fill="#0A0A0A"/>
                    <circle cx="39" cy="23" r="2" fill="#0A0A0A"/>
                    <!-- 嘴巴（橙色喙） -->
                    <ellipse cx="32" cy="29" rx="3" ry="2" fill="#F5A623" stroke="#0A0A0A" stroke-width="0.6"/>
                    <!-- 围巾（红） -->
                    <path d="M14 30 Q32 38 50 30 L48 35 Q32 42 16 35 Z" fill="#E84C3D" stroke="#A03327" stroke-width="0.8"/>
                    <!-- 翅膀/手臂（黑） -->
                    <ellipse cx="14" cy="42" rx="5" ry="9" fill="#0A0A0A"/>
                    <ellipse cx="50" cy="42" rx="5" ry="9" fill="#0A0A0A"/>
                    <!-- 脚（橙色） -->
                    <ellipse cx="26" cy="60" rx="5" ry="2.5" fill="#F5A623" stroke="#0A0A0A" stroke-width="0.6"/>
                    <ellipse cx="38" cy="60" rx="5" ry="2.5" fill="#F5A623" stroke="#0A0A0A" stroke-width="0.6"/>
                </svg>
            </span>
            <div class="qq-banner-text">
                <span class="qq-banner-label">QQ 交流群</span>
                <span id="qqGroup" class="qq-banner-num select-all">610931699</span>
            </div>
            <button id="copyQqBtn" type="button" class="qq-banner-copy" title="复制群号">
                <span class="copy-label">copy</span>
                <span class="copied-label" style="display:none">✓ 已复制</span>
            </button>
        </div>
    </section>

    <!-- 主 CTA -->
    <section class="glass glass-bright p-6 sm:p-8 rise d3">
        <div class="flex items-start gap-3 mb-5">
            <div class="font-mono text-[11px] text-[var(--ink-3)] leading-relaxed">
                <div><span class="text-[var(--accent)]">→</span> <span class="text-[var(--amber)]">action</span>: <span class="text-white">generate_report</span></div>
                <div class="opacity-70 ml-4">args: <span class="text-[var(--ink-2)]">[uid, cookies, profile]</span></div>
            </div>
        </div>
        <div class="mb-5">
            <div class="flex items-baseline gap-3 flex-wrap">
                <h2 class="font-display text-2xl sm:text-[28px] font-extrabold text-white tracking-tight">AI 测评编程能力报告</h2>
                <span class="font-mono text-[11px] text-[var(--ink-3)]">// ~3 min · 3 versions</span>
            </div>
            <p class="text-[13.5px] text-[var(--ink-2)] mt-1.5 leading-relaxed">
                填写 UID + 信息学奖项，AI 抓取洛谷做题数据，生成
                <span class="text-[var(--accent)]">选手版</span> /
                <span class="text-[var(--rose)]">家长订阅版</span> /
                <span class="text-[var(--accent-2)]">教练版</span>
                三份报告：助力选手精准训练、家长生涯规划、教练科学指导。
            </p>
        </div>
        <a href="/generate-form" class="btn-primary">
            🚀 立即生成我的学习报告
            <span class="font-mono text-[12px] opacity-80 ml-1">↵</span>
        </a>
        <div class="mt-3 flex items-center justify-between text-[12px] text-[var(--ink-3)] flex-wrap gap-2">
            <span class="font-mono">// 不需先注册 · UID + 报名信息一次填</span>
            <a href="/select-mode" class="text-[var(--accent)] hover:underline font-medium">👀 我已注册 · 直接看历史报告 →</a>
        </div>

        <!-- 老用户 UID 快速进入（嵌入主 CTA 内，更显眼） -->
        <div class="mt-5 pt-5 border-t border-dashed border-[var(--line-2)]">
            <div class="font-mono text-[10.5px] text-[var(--ink-3)] mb-2">// 已注册用户 · 直接进个人中心</div>
            <form id="me-entry" action="/me/0" method="get" class="flex gap-2" onsubmit="event.preventDefault(); var u=document.getElementById('meUid').value.trim(); if(u && /^\d{6,10}$/.test(u)) window.location.href='/me/'+u; else alert('请输入 6-10 位洛谷 UID');">
                <input id="meUid" type="text" inputmode="numeric" pattern="\\d{6,10}" placeholder="洛谷 UID（6-10 位数字）" class="field flex-1">
                <button type="submit" class="btn-secondary whitespace-nowrap font-mono">进入 ›</button>
            </form>
        </div>
    </section>

    <!-- 3 大特性 -->
    <section class="grid grid-cols-1 sm:grid-cols-3 gap-3 rise d4">
        <div class="feat">
            <div class="num">// NOI_01</div>
            <div class="ic">🌳</div>
            <h3>知识树图谱</h3>
            <p>按 CSP-J / CSP-S / 省选 / NOI 四个级别画 4 棵知识树，果子大小 = 掌握度，一眼看出盲区。</p>
        </div>
        <div class="feat violet">
            <div class="num">// NOI_02</div>
            <div class="ic">🧠</div>
            <h3>AI 深度解读</h3>
            <p>大模型阅读全部做题记录，输出 AI 定级、性格画像、高频算法雷达、阶段成长曲线。</p>
        </div>
        <div class="feat pink">
            <div class="num">// NOI_03</div>
            <div class="ic">📨</div>
            <h3>家长订阅版</h3>
            <p>一份"家长看得懂"的报告：非技术语言 + 学习建议 + 关键事件解读，可订阅每周推送。</p>
        </div>
    </section>

    <!-- 强基 39 校 · 滚动展示（校徽 + 校名） -->
    <section class="ticker rise d5" aria-label="强基计划 39 所高校">
        <div class="ticker-track ticker-schools">
            {# v3.7/v3.8/v3.9 强基 39 校：PNG 校徽 + 缺失时彩色圆圈占位（校色+拼音缩写） #}
            {% set qiangji_schools = [
                ('pku',   '北京大学',         '#A40027', 'PKU',  '北京'),
                ('thu',   '清华大学',         '#660874', 'THU',  '北京'),
                ('ruc',   '中国人民大学',     '#C8161D', 'RUC',  '北京'),
                ('buaa',  '北京航空航天大学', '#0050B3', 'BUAA', '北京'),
                ('bit',   '北京理工大学',     '#1A6E3A', 'BIT',  '北京'),
                ('cau',   '中国农业大学',     '#D4A017', 'CAU',  '北京'),
                ('bnu',   '北京师范大学',     '#003D7C', 'BNU',  '北京'),
                ('muc',   '中央民族大学',     '#3F4A5C', 'MUC',  '北京'),
                ('nankai','南开大学',         '#591F5C', 'NKU',  '天津'),
                ('tju',   '天津大学',         '#005BAA', 'TJU',  '天津'),
                ('dlut',  '大连理工大学',     '#006747', 'DUT',  '辽宁'),
                ('neu',   '东北大学',         '#C9A227', 'NEU',  '辽宁'),
                ('jlu',   '吉林大学',         '#9B1D20', 'JLU',  '吉林'),
                ('hit',   '哈尔滨工业大学',   '#1F3A93', 'HIT',  '黑龙江'),
                ('fdu',   '复旦大学',         '#B71C2A', 'FDU',  '上海'),
                ('tongji','同济大学',         '#003E7E', 'TJ',   '上海'),
                ('sjtu',  '上海交通大学',     '#0A246A', 'SJTU', '上海'),
                ('ecnu',  '华东师范大学',     '#0B6E4F', 'ECNU', '上海'),
                ('nju',   '南京大学',         '#6B2A78', 'NJU',  '江苏'),
                ('seu',   '东南大学',         '#D4A017', 'SEU',  '江苏'),
                ('zju',   '浙江大学',         '#B71C2A', 'ZJU',  '浙江'),
                ('ustc',  '中国科学技术大学', '#C0392B', 'USTC', '安徽'),
                ('xmu',   '厦门大学',         '#B8923A', 'XMU',  '福建'),
                ('sdu',   '山东大学',         '#003E7E', 'SDU',  '山东'),
                ('ouc',   '中国海洋大学',     '#005BAA', 'OUC',  '山东'),
                ('whu',   '武汉大学',         '#591F5C', 'WHU',  '湖北'),
                ('hust',  '华中科技大学',     '#0B6E4F', 'HUST', '湖北'),
                ('csu',   '中南大学',         '#D4A017', 'CSU',  '湖南'),
                ('hnu',   '湖南大学',         '#9B1D20', 'HNU',  '湖南'),
                ('nudt',  '国防科技大学',     '#0B5345', 'NUDT', '湖南'),
                ('sysu',  '中山大学',         '#005BAA', 'SYSU', '广东'),
                ('scut',  '华南理工大学',     '#B71C2A', 'SCUT', '广东'),
                ('scu',   '四川大学',         '#C9A227', 'SCU',  '四川'),
                ('cqu',   '重庆大学',         '#1F3A93', 'CQU',  '重庆'),
                ('uestc', '电子科技大学',     '#1F3A93', 'UESTC','四川'),
                ('xjtu',  '西安交通大学',     '#B71C2A', 'XJTU', '陕西'),
                ('nwpu',  '西北工业大学',     '#005BAA', 'NPU',  '陕西'),
                ('nwafu', '西北农林科技大学', '#0B6E4F', 'NWAFU','陕西'),
                ('lzu',   '兰州大学',         '#005BAA', 'LZU',  '甘肃'),
            ] %}
            {% for s in qiangji_schools %}
            <span class="school-chip" title="{{ s[1] }} · {{ s[4] }}">
                <img class="school-img" src="/static/schools/{{ s[0] }}.png" alt="{{ s[1] }}" loading="lazy">
                <span class="school-name">{{ s[1] }}</span>
            </span>
            {% endfor %}
            {# 复制一份用于无缝循环滚动 #}
            {% for s in qiangji_schools %}
            <span class="school-chip" title="{{ s[1] }} · {{ s[4] }}" aria-hidden="true">
                <img class="school-img" src="/static/schools/{{ s[0] }}.png" alt="{{ s[1] }}" loading="lazy">
                <span class="school-name">{{ s[1] }}</span>
            </span>
            {% endfor %}
        </div>
    </section>

    <!-- 客服/教练版 -->
    {% if not commerce_hidden %}
    <section class="grid grid-cols-1 md:grid-cols-2 gap-3 rise d6">
        <div class="contact-card amber">
            <div class="flex items-center justify-between mb-2">
                <div class="font-mono text-[10.5px] text-[var(--ink-3)]">// CONTACT_PARENT</div>
                <span class="font-mono text-[10px] text-[var(--amber)]">vip</span>
            </div>
            <div class="text-[15px] font-bold text-[var(--amber)] mb-1.5 font-display">📱 家长 / 讲题 加 V</div>
            <p class="text-[12px] text-[var(--ink-2)] mb-3 leading-relaxed">加客服微信，回复「家长」或「讲题」领取兑换码</p>
            <div class="flex items-center gap-2 bg-[rgba(0,0,0,.35)] border border-[rgba(255,182,39,.25)] rounded-lg px-3 py-2">
                <span class="text-[11px] text-[var(--ink-3)]">wx:</span>
                <span class="font-mono font-bold text-[var(--amber)] select-all flex-1" id="wechatVip">xinjing-ai-vip</span>
                <button id="copyVipBtn" type="button" class="px-2 py-0.5 rounded-md border border-[rgba(255,182,39,.3)] text-[var(--amber)] hover:bg-[rgba(255,182,39,.08)] text-[11px] font-mono">copy</button>
            </div>
            <p class="text-[11px] text-[var(--ink-3)] mt-2 font-mono">// 9:00-21:00 workdays · 10:00-18:00 holidays</p>
        </div>
        <div class="contact-card gray">
            <div class="flex items-center justify-between mb-2">
                <div class="font-mono text-[10.5px] text-[var(--ink-3)]">// CONTACT_COACH</div>
                <span class="font-mono text-[10px] text-[var(--accent-2)]">B2B</span>
            </div>
            <div class="text-[15px] font-bold text-white mb-1.5 font-display">🏢 教练版 / 机构合作</div>
            <p class="text-[12px] text-[var(--ink-2)] mb-3 leading-relaxed">批量学员管理 · 兑换码生成 · 营收看板</p>
            <div class="space-y-1 text-[12px] text-[var(--ink-2)] font-mono">
                <div>📞 <span class="text-white">400-XXX-XXXX</span></div>
                <div>📧 <span class="text-white">coach@xinjing-ai.com</span></div>
                <div>📋 按学员数计费 · <a href="/coach" class="text-[var(--accent)] hover:underline">查看详情 →</a></div>
            </div>
        </div>
    </section>
    {% endif %}

    <!-- 底栏 -->
    <footer class="text-center text-[11px] text-[var(--ink-3)] font-mono pt-2 rise d6">
        <span>© 2026 信竞 AI 报告 · Luogu-AI-Report</span>
    </footer>

</main>

<script>
    // 顶栏时钟
    (function(){
        var el=document.getElementById('statusbarClock');
        if(!el) return;
        function tick(){
            var d=new Date();
            var p=function(n){return (n<10?'0':'')+n};
            el.textContent=p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());
        }
        tick();setInterval(tick,1000);
    })();

    // 复制通用（支持按钮内嵌 .copy-label / .copied-label 两个 span）
    function bindCopy(btnId,textId,okMsg,failMsg){
        var btn=document.getElementById(btnId);
        var textEl=document.getElementById(textId);
        if(!btn||!textEl) return;
        var copyLabel=btn.querySelector('.copy-label');
        var copiedLabel=btn.querySelector('.copied-label');
        var hasDualLabel=!!(copyLabel&&copiedLabel);
        btn.addEventListener('click', async function(){
            var value=(textEl.textContent||'').trim();
            var done=false;
            try{
                if(navigator.clipboard&&navigator.clipboard.writeText){
                    await navigator.clipboard.writeText(value);
                }else{
                    var ta=document.createElement('textarea');
                    ta.value=value;ta.style.position='fixed';ta.style.top='-1000px';
                    document.body.appendChild(ta);ta.focus();ta.select();
                    document.execCommand('copy');document.body.removeChild(ta);
                }
                done=true;
            }catch(e){}
            if(hasDualLabel){
                copyLabel.style.display='none';
                copiedLabel.style.display='inline';
                copiedLabel.textContent=done?('✓ '+okMsg):failMsg;
            }else{
                btn.textContent=done?okMsg:failMsg;
            }
            setTimeout(function(){
                if(hasDualLabel){
                    copyLabel.style.display='';
                    copiedLabel.style.display='none';
                }else{
                    btn.textContent='copy';
                }
            },1400);
        });
    }
    bindCopy('copyQqBtn','qqGroup','copied!','failed');
    bindCopy('copyVipBtn','wechatVip','copied!','failed');
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
    # v3.9.5 · GESP 自录奖项持久化：之前没存到 retry_form_json，导致学员
    # "明明填了 GESP 却没看到段位" 时无法回放/恢复。
    # 加上这 4 个字段后，_backfill_gesp_v395 就能从历史 task 里找回 GESP 数据。
    "gesp_level",
    "gesp_score",
    "gesp_year",
    "gesp_certificate_no",
    # v3.9.5 · CSP/NOIP/NOI 自录奖项持久化（同样原因）
    "csp_competition_type",
    "csp_award_level",
    "csp_award_year",
    "csp_score",
    "csp_province",
    # v3.9.14 · 选手档案字段持久化：省份/城市/性别/出生日期
    # 之前没存到 retry_form_json，导致「返回重试」后这些字段全空，
    # 用户不得不重新填（之前是写入 students 表，但 retry_form_json 没存）
    "province",
    "city",
    "city_legacy",
    "gender",
    "birth_date",
)


def _detect_401_invalid_api_key(task: dict | None) -> bool:
    """v3.9.11 · 检测任务失败原因是否为 OpenAI 401 invalid api key

    任务 message 字段会记录错误，例如：
      "[阶段: 生成 AI 报告] AuthenticationError: ... 'message': 'invalid api key ...' [status code=401] ..."
    """
    if not isinstance(task, dict):
        return False
    msg = str(task.get("message", "") or "")
    if not msg:
        return False
    needles = ("401", "invalid api key", "authentication_error", "Incorrect API key")
    return any(n in msg for n in needles)


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
    # v3.9.6 · 已登录学员：在首页顶部加一个 "已登录为 XX" 横幅 + 直达个人中心
    logged_in_banner = ""
    try:
        _uid = str(session.get("student_uid") or "").strip()
        _name = str(session.get("student_name") or "").strip()
        if _uid and _uid.isdigit():
            # v3.9.20 · 首页用深空主题，app-btn-primary 的白字+绿底在首页 CSS 里没注册，
            # 退化为浏览器默认 <a> 蓝色，文字就看不见。改用 Tailwind 显式指定绿底白字。
            logged_in_banner = (
                f'<div class="bg-emerald-500/10 border border-emerald-400/40 rounded-lg p-3 mb-4 flex items-center justify-between gap-3">'
                f'<div class="text-sm text-emerald-100">✅ 已识别身份：<strong class="text-white">{_name or "学员"}</strong>（UID {_uid}）</div>'
                f'<a href="/me/{_uid}" class="inline-flex items-center justify-center px-3 py-1.5 rounded-md text-xs font-bold bg-gradient-to-r from-emerald-500 to-teal-500 text-white hover:from-emerald-400 hover:to-teal-400 whitespace-nowrap shadow-lg shadow-emerald-500/30">🎓 回我的个人中心 →</a>'
                f'</div>'
            )
    except Exception:
        pass
    return render_template_string(
        INDEX_HTML,
        form_values=build_form_values(form),
        validation_result=validation_result,
        server_key_hint=server_key_hint,
        commerce_hidden=_HIDE_COMMERCE,
        logged_in_banner=logged_in_banner,
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
    luogu_uid: str = "",  # v3.8 · 注入档案 + 奖项 + 政策
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
                    luogu_uid=luogu_uid,  # v3.8 · 注入档案 + 奖项 + 政策匹配
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
            app.logger.warning(f"normalize_report_markdown failed in main flow: {_norm_err}")

    current_stage = "生成图表与 HTML/PDF"
    chart_paths = generate_chart_images(export_data, str(assets_dir))
    build_html_and_pdf(report_md, export_data, str(html_path), str(pdf_path), chart_paths)

    eval_time = _NOW_BJ().strftime("%Y-%m-%d %H:%M")
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

    # v3.7 · 报告生成后立刻标记 hide_pdf=1（统一走海报扫码，不开放 PDF 直链）
    _record_hide_pdf(task_id)

    # v3.8 · 报告生成完成时预渲染分享海报 PNG → 写入 report_dir/share-card.png
    # 用户点击"📤 生成海报分享"时直接读取缓存（O(1)），不再现场 matplotlib 渲染（5-15s）
    # v3.9 · 修复 v3.8 的 NameError bug：函数形参叫 luogu_uid，没有 form 局部变量
    try:
        _cached_uid = (str(luogu_uid) or "").strip()
        if not _cached_uid and out_dir and out_dir.exists():
            # 从目录名兜底提取（目录命名规则：<real_name>_<uid>_<timestamp>）
            _dir_name = out_dir.name
            _parts = _dir_name.split("_")
            if len(_parts) >= 2 and _parts[-2].isdigit():
                _cached_uid = _parts[-2]
        if _cached_uid:
            _sc_data = _build_share_card_data(_cached_uid)
            if _sc_data:
                # qr_url 用 base URL（让本地和线上都能扫码）
                _base = request.host_url.rstrip("/") if request else ""
                if not _base:
                    _base = "https://oi.aijiangti.cn"  # v3.8 · 部署默认域名
                _sc_qr = f"{_base}/r/{_cached_uid}"
                _sc_png = _render_share_card_png(_sc_data, _sc_qr)
                _sc_path = out_dir / "share-card.png"
                _sc_path.write_bytes(_sc_png)
                app.logger.info(f"v3.8 share-card.png cached: {_sc_path} ({len(_sc_png)} bytes)")
    except Exception as _sc_err:
        app.logger.warning(f"v3.8 预渲染分享海报失败（不影响主流程）: {_sc_err}")


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
        # v3.8 · 自动补全学员档案（保证 AI 报告 / 海报用得到 city/school/province）
        _form_uid = str(form.get("luogu_uid") or form.get("uid") or "").strip()
        if _form_uid:
            _auto_upsert_student_profile(_form_uid, form)

        with TASKS_LOCK:
            update_task(task_id, status="running", message="正在连接洛谷 API...")

        api_key, api_key_source = resolve_openai_api_key(form)
        base_url = form.get("base_url", "").strip() or DEFAULT_BASE_URL or os.environ.get("OPENAI_BASE_URL", "") or None
        model_name = form.get("model_name", "").strip() or DEFAULT_MODEL_NAME or os.environ.get("OPENAI_MODEL_NAME", "") or "gpt-4o"
        # v3.8 · 全量抓取默认开启，单侧上限 3000 题（防止极端账号拖死服务）
        MAX_FETCH_LIMIT = 3000
        try:
            max_passed = int(form.get("max_passed", MAX_FETCH_LIMIT) or MAX_FETCH_LIMIT)
        except (TypeError, ValueError):
            max_passed = MAX_FETCH_LIMIT
        try:
            max_failed = int(form.get("max_failed", MAX_FETCH_LIMIT) or MAX_FETCH_LIMIT)
        except (TypeError, ValueError):
            max_failed = MAX_FETCH_LIMIT
        max_passed = max(0, min(max_passed, MAX_FETCH_LIMIT))
        max_failed = max(0, min(max_failed, MAX_FETCH_LIMIT))
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
            task_id, student_name, luogu_uid=str(form.get("luogu_uid") or form.get("uid") or "").strip()
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
                luogu_uid=str(form.get("luogu_uid", "") or "").strip(),  # v3.8 · 注入档案
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
        # v3.8 · 先尝试命中磁盘缓存（标签全局，作业记录 6h TTL），避免每次重试都重拉
        cached_tag_by_id, cached_type_by_id, tag_cached_at = _load_cached_tag_maps()
        if cached_tag_by_id is not None:
            tag_by_id, type_by_id = cached_tag_by_id, cached_type_by_id
            with TASKS_LOCK:
                update_task(
                    task_id,
                    message=f"✅ 命中标签缓存（{tag_cached_at}），跳过洛谷拉取，秒进下一步",
                )
        else:
            tag_by_id, type_by_id = _build_tag_maps(luogu)
            _save_cached_tag_maps(tag_by_id, type_by_id)

        cached_practice_data = _load_cached_practice(uid)
        if cached_practice_data is not None:
            # v3.8 · 构造一个轻量代理对象（split_practice_problems 只用 .data）
            class _PracticeProxy:
                __slots__ = ("data",)

                def __init__(self, data):
                    self.data = data

            practice = _PracticeProxy(cached_practice_data)
            with TASKS_LOCK:
                update_task(
                    task_id,
                    message=f"✅ 命中作业记录缓存（6h TTL），跳过洛谷拉取，秒进下一步",
                )
        else:
            practice = luogu.get_user_practice(uid)
            _save_cached_practice(uid, practice)

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
                # v3.9.7 · 中文年级（不再显示 PRIMARY_6 这种 enum 值）
                "grade": _grade_to_label(grade) or grade,
                "grade_zh": _grade_to_label(grade) or grade,
                "eval_time": _NOW_BJ().strftime("%Y-%m-%d %H:%M"),
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
            luogu_uid=_form_uid,  # v3.8 · 注入档案（已含 luogu_uid/uid 兜底）
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
    # v3.7 · ref 归因 cookie（30 天）
    raw_ref = request.args.get("ref")
    sanitized_ref = _sanitize_ref(raw_ref) if raw_ref else ""

    response = make_response(render_index())
    if sanitized_ref:
        response.set_cookie(
            "ref_uid", sanitized_ref,
            max_age=30 * 24 * 3600,
            httponly=True,
            samesite="Lax",
        )
    return response


@app.route("/validate-cookies", methods=["POST"])
def validate_cookies_page():
    form = request.form.to_dict()
    return render_index(form=form, validation_result=validate_cookies(form))


@app.route("/generate", methods=["POST"])
def generate():
    form_data = request.form.to_dict()
    task_id = str(uuid.uuid4())
    # v3.8 · 取 UID（兜底 luogu_uid/uid 两种字段名）供 tasks.luogu_uid 写入
    _g_uid = str(form_data.get("luogu_uid") or form_data.get("uid") or "").strip()
    with TASKS_LOCK:
        insert_task(task_id, status="queued", message="排队中...", luogu_uid=_g_uid)
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
    {# v3.9 · 任务完成后停止自动刷新（避免用户填邀请码/操作时输入被清空） #}
    {% if status != 'done' and status != 'error' %}
    <meta http-equiv="refresh" content="3">
    {% endif %}
    {{ app_skin_head() }}
</head>
<body class="app-body p-4">
    <div class="max-w-2xl mx-auto py-6 space-y-4">
        <div class="app-card text-center">
            <h1 class="app-title">📊 报告生成状态</h1>
            <p class="app-subtitle">AI 正在生成报告，请不要关闭本页面</p>
        </div>
        <div class="app-card text-center">
            <div class="mb-4">
                <span class="app-pill {% if status == 'done' %}app-pill-done{% elif status == 'error' %}app-pill-error{% else %}app-pill-running{% endif %}">
                    {{ '✅ 完成' if status == 'done' else ('❌ 失败' if status == 'error' else '⏳ 进行中') }}
                </span>
            </div>
        {% if source_code_total and source_code_total|int > 0 %}
        <div class="mb-4 text-left">
            <div class="flex items-center justify-between text-sm text-gray-600 mb-1">
                <span>源码获取进度</span>
                <span class="font-semibold text-gray-800">{{ source_code_success }}/{{ source_code_total }}</span>
            </div>
            <div class="app-progress">
                <div class="app-progress-fill" style="width: {{ (100 * (source_code_success|int) / (source_code_total|int)) if (source_code_total|int) > 0 else 0 }}%;"></div>
            </div>
        </div>
        {% endif %}
        {% if tag_fetch_total and tag_fetch_total|int > 0 %}
        <div class="mb-4 text-left">
            <div class="flex items-center justify-between text-sm text-gray-600 mb-1">
                <span>标签补全进度</span>
                <span class="font-semibold text-gray-800">{{ tag_fetch_success }}/{{ tag_fetch_total }}</span>
            </div>
            <div class="app-progress">
                <div class="app-progress-fill" style="width: {{ (100 * (tag_fetch_success|int) / (tag_fetch_total|int)) if (tag_fetch_total|int) > 0 else 0 }}%;"></div>
            </div>
        </div>
        {% endif %}
        {% if stage == '生成 AI 报告' %}
        <div class="mb-4 text-left">
            <div class="flex items-center justify-between text-sm text-gray-600 mb-1">
                <span>AI 报告生成进度</span>
                <span class="font-semibold text-gray-800">{{ ai_progress }}%{% if ai_elapsed_seconds and ai_elapsed_seconds|int > 0 %} · {{ ai_elapsed_seconds }}s{% endif %}</span>
            </div>
            <div class="app-progress">
                <div class="app-progress-fill" style="width: {{ ai_progress|int }}%;"></div>
            </div>
        </div>
        {% endif %}
        {# v3.9.14 · AI 服务拒绝请求的通用提示（仅当本轮以 401 失败时显示，running/completed 不显示） #}
        {% if is_401_api_key and status == 'error' %}
        <div class="mb-4 rounded-lg border-2 border-amber-300 bg-amber-50 p-4 text-left text-sm">
            <p class="font-bold text-amber-800 mb-1">⚠️ AI 服务返回 401（拒绝请求）</p>
            <ul class="text-amber-700 text-xs space-y-1 list-disc list-inside">
                <li>可能原因：API Key / Base URL / 模型名 任一不匹配 · 临时限流 · 该模型当下不可用</li>
                <li>👉 点击下方「返回表单」后检查 12 项字段（已自动回填）</li>
                <li>可先点「立即生成」重试一次，**401 多为临时问题**，重试通常可解决</li>
            </ul>
        </div>
        {% endif %}
        <p class="text-gray-700 mb-6">{{ message }}</p>
        {% if task_type == 'parent_subscribe' %}
        <div class="mb-4 text-left">
            <div class="flex items-center justify-between text-sm text-gray-600 mb-1">
                <span>📨 家长订阅版 AI 生成进度</span>
                <span class="font-semibold text-gray-800">{{ ai_progress }}%{% if ai_elapsed_seconds and ai_elapsed_seconds|int > 0 %} · {{ ai_elapsed_seconds }}s{% endif %}</span>
            </div>
            <div class="app-progress">
                <div class="app-progress-fill" style="width: {{ ai_progress|int }}%;"></div>
            </div>
        </div>
        {% if status == 'done' %}
        {# v3.9.6 · 去掉 Markdown 原文按钮（家长不应直接看源码）；重命名"AI 真生成" → "AI 决策支持" #}
        <a href="{{ ps_html }}" target="_blank" class="app-btn app-btn-amber mb-2">📨 查看家长订阅版（AI 决策支持）</a>
        <a href="/me/{{ luogu_uid }}/parent-subscribe" class="app-btn app-btn-secondary">↩ 返回家长订阅版页</a>
        {% elif status == 'error' %}
        <a href="/me/{{ luogu_uid }}/parent-subscribe" class="app-btn app-btn-primary">返回重试</a>
        {% else %}
        <p class="text-sm text-gray-400">页面每 3 秒自动刷新，AI 正在基于您家孩子的报告重写一份家长视角的深度分析...</p>
        {% endif %}
        {% elif status == 'done' %}
        <div class="space-y-3">
            {# v3.8 · 用户态报告页：HTML + 海报分享 + 家长订阅版（PDF/Markdown 隐藏到 /admin 后台） #}
            <div class="grid grid-cols-2 gap-3">
                <a href="{{ html }}" target="_blank" class="app-btn app-btn-primary">🔍 查看 HTML 报告</a>
                <button type="button" onclick="openSharePoster()" class="app-btn app-btn-amber">📤 生成海报分享</button>
            </div>
            {% if me_url %}
            {# v3.9.6 · 智能门控：已生成过家长订阅版 → 直接显示"查看"，不再每次让家长重输邀请码 #}
            {% if has_parent_sub_html %}
                <div class="bg-emerald-50 border border-emerald-200 rounded-lg p-3">
                    <p class="text-sm text-emerald-800">✅ 您家孩子的家长订阅版已生成</p>
                    <a href="{{ ps_html_url }}" target="_blank" class="app-btn app-btn-amber mt-2 block text-center">📨 查看家长订阅版（AI 决策支持）</a>
                    <a href="/me/{{ me_url.split('/')[-1] }}/parent-subscribe" class="app-btn app-btn-secondary mt-2 block text-center">↩ 进入家长订阅版中心</a>
                </div>
            {% else %}
            {# v3.9 · 家长订阅版：邀请码门控（获取方式 = 加微信） #}
            <form method="POST" action="/me/{{ me_url.split('/')[-1] }}/start-parent-subscribe" class="block">
                <div class="bg-amber-50 border border-amber-200 rounded-lg p-3 mb-2">
                    <label class="block text-xs font-bold text-amber-800 mb-1">🔑 家长订阅邀请码（必填）</label>
                    <input type="text" name="invite_code" required
                           placeholder="扫码下方微信，备注'家长订阅'获取"
                           class="w-full px-3 py-2 border border-amber-300 rounded text-sm font-mono focus:outline-none focus:border-amber-500" />
                    <div class="flex items-start gap-3 mt-3">
                        <img src="/static/wechat_qr.png" alt="微信二维码"
                             class="w-28 h-28 border border-amber-200 rounded bg-white p-1 flex-shrink-0" />
                        <div class="text-[11px] text-amber-700 leading-relaxed flex-1">
                            📞 <strong>邀请码获取方式：</strong>
                            {# v3.9.24 · 改 list-inside + 去掉 pl-4：旧版 numbers 排在 li 外面，配合 pl-4 把序号挤到左侧 QR 区域，看上去「1.2.3.4.」和文字分两列。list-inside 把序号收回文字前，与 li 文本对齐。 #}
                            <ol class="list-decimal list-inside mt-1 space-y-0.5 marker:font-bold marker:text-amber-800">
                                <li>微信扫码左侧二维码</li>
                                <li>添加客服为好友</li>
                                <li>备注"<strong>家长订阅</strong>"</li>
                                <li>客服会立即发送邀请码</li>
                            </ol>
                        </div>
                    </div>
                </div>
                <button type="submit" class="app-btn app-btn-amber">
                    📨 验证邀请码并生成家长订阅版
                </button>
                <p class="text-[10px] text-gray-500 text-center mt-1">💡 使用服务端环境变量 OPENAI_API_KEY 直接生成（约 1-2 分钟）</p>
            </form>
            {% endif %}
            {% endif %}
        </div>

        {# v3.8 · 海报分享模态框（点击右上方"生成海报分享"触发） #}
        <div id="posterModal" class="hidden fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4"
             onclick="if(event.target===this) closeSharePoster()">
            <div class="bg-white rounded-2xl shadow-2xl max-w-md w-full p-5 relative">
                <button type="button" onclick="closeSharePoster()" class="absolute top-3 right-3 w-8 h-8 rounded-full bg-gray-100 hover:bg-gray-200 text-gray-600 flex items-center justify-center text-lg">×</button>
                <h3 class="text-lg font-bold text-gray-800 mb-1 text-center">📤 分享海报</h3>
                <p class="text-xs text-gray-500 text-center mb-3">首次生成约 5-15 秒，生成后自动下载</p>
                <div class="flex justify-center bg-gray-50 border border-gray-200 rounded-lg p-2 mb-3 min-h-[200px] items-center relative">
                    <img id="posterImg" src="" alt="学习报告海报"
                         class="max-w-full h-auto rounded shadow"
                         style="display:none"
                         onerror="this.style.display='none'; var eb=document.getElementById('posterError'); if(eb){eb.textContent='海报加载失败';eb.style.display='';}" />
                    <div id="posterLoading" class="text-center text-gray-500">
                        <div class="inline-block w-10 h-10 border-4 border-emerald-500 border-t-transparent rounded-full animate-spin mb-2"></div>
                        <p class="text-sm">海报生成中…</p>
                        <p class="text-[10px] text-gray-400 mt-1">首次需要 matplotlib 渲染，约 5-15 秒</p>
                    </div>
                    <div id="posterError" class="text-center text-rose-600 text-sm" style="display:none"></div>
                </div>
                <div class="flex gap-2">
                    <a id="posterDownloadBtn" href="/me/{{ luogu_uid }}/share-card.png" download="学习报告海报_{{ luogu_uid }}.png"
                       class="app-btn app-btn-primary flex-1">⬇ 再次下载</a>
                    <button type="button" onclick="closeSharePoster()" class="app-btn app-btn-secondary flex-1">关闭</button>
                </div>
            </div>
        </div>
        <script>
        (function(){
            function openSharePoster(){
                var m=document.getElementById('posterModal');
                var img=document.getElementById('posterImg');
                var loading=document.getElementById('posterLoading');
                var errorBox=document.getElementById('posterError');
                var btn=document.getElementById('posterDownloadBtn');
                if(!m||!img) return;
                // 初始态：loading 显、img 隐、error 隐
                if(loading) loading.style.display='';
                if(errorBox) errorBox.style.display='none';
                img.style.display='none';
                m.classList.remove('hidden');
                // 1) 预加载海报 PNG（matplotlib 现场渲染，可能 5-15s）
                var url='/me/{{ luogu_uid }}/share-card.png?t='+Date.now();
                var pre=new Image();
                pre.onload=function(){
                    // 2) 加载完成 → 显示 + 自动下载
                    img.src=url;
                    img.style.display='';
                    if(loading) loading.style.display='none';
                    if(errorBox) errorBox.style.display='none';
                    triggerDownload(url);
                };
                pre.onerror=function(){
                    // 3) 失败：loading 隐、error 显（提示重试）
                    if(loading) loading.style.display='none';
                    img.style.display='none';
                    if(errorBox){
                        errorBox.textContent='海报生成失败（HTTP '+(pre.failedStatus||'?')+'）· 请稍后重试或联系管理员';
                        errorBox.style.display='';
                    }
                };
                pre.src=url;

                function triggerDownload(finalUrl){
                    try{
                        // 优先复用"再次下载"按钮（带 download 属性）
                        if(btn){
                            btn.href=finalUrl;
                            btn.setAttribute('download','学习报告海报_{{ luogu_uid }}.png');
                            btn.click();
                            return;
                        }
                    }catch(e){}
                    // 兜底：构造临时 a 标签
                    try{
                        var a=document.createElement('a');
                        a.href=finalUrl;
                        a.download='学习报告海报_{{ luogu_uid }}.png';
                        a.style.display='none';
                        document.body.appendChild(a);a.click();document.body.removeChild(a);
                    }catch(e){console.error('[poster download]',e);}
                }
            }
            function closeSharePoster(){
                var m=document.getElementById('posterModal');
                if(m) m.classList.add('hidden');
            }
            window.openSharePoster=openSharePoster;
            window.closeSharePoster=closeSharePoster;
            document.addEventListener('keydown',function(e){if(e.key==='Escape')closeSharePoster();});
        })();
        </script>
        {% elif status == 'error' %}
        <a href="{{ retry_url }}" class="app-btn app-btn-primary mt-4">🔁 返回表单（已自动回填）</a>
        {% if me_url %}
        {# 错误状态也用 POST 表单，确保点击直接重试 #}
        <form method="POST" action="/me/{{ me_url.split('/')[-1] }}/start-parent-subscribe" class="block mt-2">
            <button type="submit" class="app-btn app-btn-amber">
                📨 重试家长订阅版
            </button>
        </form>
        {% endif %}
        {% else %}
        <p class="text-sm text-gray-400">页面每 3 秒自动刷新...</p>
        {% endif %}
        </div>
        <p class="text-center text-xs text-gray-500">
            <a href="/" class="app-link">← 返回首页</a>
        </p>
    </div>
</body>
</html>
"""


# v3.8 · 报告列表的「单行模板」（admin 后台用）
# - HTML / PDF / MD 全部可下载（用户态报告页已隐藏 PDF/MD，统一走海报）
# - 状态 pill / 链接 统一 emerald 主色（与首页一致）
LIST_REPORTS_HTML = """
{# 单个报告在列表中的一行（含 HTML / PDF / MD 三个操作 pill） #}
<td class="px-6 py-3 space-x-2">
  {% if task.html %}
  <a href="{{ task.html }}" target="_blank" class="text-emerald-700 hover:underline text-xs font-semibold">HTML</a>
  {% endif %}
  {# v3.8 · admin 后台可下载 PDF（仅后台开放） #}
  {% if task.pdf %}
  <a href="{{ task.pdf }}" download class="text-emerald-700 hover:underline text-xs font-semibold" title="v3.8 仅管理员可下载 PDF">PDF</a>
  {% endif %}
  {% if task.md %}
  <a href="{{ task.md }}" target="_blank" class="text-emerald-700 hover:underline text-xs font-semibold">MD</a>
  {% endif %}
  {% if task.can_rebuild %}
  <form method="post" action="/admin/rebuild-html/{{ task.id }}" class="inline">
    <button type="submit" class="text-xs text-emerald-700 hover:underline font-semibold">重建 HTML</button>
  </form>
  {% endif %}
</td>
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

    # v3.9.6 · 智能门控：检查该 UID 是否已生成过 parent_subscribe.html
    # 如果已生成 → 状态页直接显示"查看家长订阅版"，不再每次让家长重输邀请码
    has_parent_sub_html = False
    ps_html_url = ""
    if luogu_uid and luogu_uid.isdigit():
        try:
            _stu = _admin_students.get_student_by_uid(luogu_uid)
            _stu_name = (_stu.get("real_name") or "") if _stu else ""
            _latest = _find_latest_report_dir(luogu_uid, _stu_name)
            if _latest and (_latest / "parent_subscribe.html").exists():
                has_parent_sub_html = True
                ps_html_url = f"/reports/{_latest.name}/parent_subscribe.html"
        except Exception as _e:
            app.logger.debug(f"[status_page] has_parent_sub check failed: {_e}")

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
        # v3.9.11 · 401 错误标记：让 status_page 顶部显示专项提示
        is_401_api_key=_detect_401_invalid_api_key(task),
        html=task.get("html", ""),
        pdf=_download_report_url(pdf_url),
        md=task.get("md", ""),
        ps_html=task.get("ps_html", ""),
        ps_md=task.get("ps_md", ""),
        retry_url=url_for("retry_task", task_id=task_id),
        me_url=me_url,
        luogu_uid=luogu_uid,
        # v3.9.6 · 新增：智能门控用
        has_parent_sub_html=has_parent_sub_html,
        ps_html_url=ps_html_url,
    )


@app.route("/retry/<task_id>")
def retry_task(task_id):
    """v3.9.10 · 报告生成失败的重试入口（保留缓存，直接回到表单页）

    行为变化：
      旧：redirect → 首页 INDEX_HTML → 用户需再次点「立即生成」→ 才看到空表
      新：直接渲染 v3.5.2 表单页 GENERATE_FORM_HTML，且自动回填上次填过的
          client_id / uid / c3vk / api_key / 姓名 / 学校 / 年级 / 城市
          用户不用重新输入，调整后直接点「立即生成」即可

    字段映射：
      - snapshot.student_name → form.real_name
      - form.city 从 students 表兜底
      - form.resume_task_id 标记为「可从 AI 阶段恢复」
    """
    task = get_task(task_id) or {}
    snapshot = load_retry_form_snapshot(task)
    if not snapshot:
        # 兜底：完全没有快照（旧 task / 链接错误），回到空表单让用户重填
        flash("未找到上次填写的表单数据，请重新填写后提交。", "warning")
        return redirect("/generate-form")

    # 字段映射：snapshot 用 student_name，v3.5.2 表单字段名是 real_name
    if not snapshot.get("real_name") and snapshot.get("student_name"):
        snapshot["real_name"] = str(snapshot.pop("student_name") or "").strip()
    elif snapshot.get("real_name") and snapshot.get("student_name"):
        # 两者都存在时优先 real_name，删掉 student_name 避免 form 渲染到不存在的字段
        snapshot.pop("student_name", None)

    # 兜底：city 从学生档案获取
    if not str(snapshot.get("city") or "").strip():
        uid = str(task.get("luogu_uid") or snapshot.get("uid") or "").strip()
        if uid:
            try:
                student = _admin_students.get_student_by_uid(uid)
                if student and student.get("city"):
                    snapshot["city"] = str(student.get("city") or "").strip()
            except Exception:
                pass

    # AI 阶段恢复标识（让 run_generation 跳过抓取，直接从 AI 接口继续）
    if can_resume_from_ai_stage(task):
        snapshot["resume_task_id"] = task_id

    # 提示
    is_401 = _detect_401_invalid_api_key(task)
    if not str(task.get("retry_form_json", "") or "").strip():
        flash(
            "已自动回填「姓名 / 学校 / 年级」，Cookies / API Key 仍需补全后再生成。",
            "warning",
        )
    elif is_401:
        # v3.9.12 · 柔化 401 提示：不再假设 Key 有问题 / 不再清空 api_key / 不再 auto-focus
        flash(
            "⚠️ AI 服务返回 401（拒绝请求）。可能是 Key/Base URL/模型 任一不匹配，"
            "或临时限流 / 该模型当下不可用。表单已自动回填，"
            "**多数情况重试一次即可**。如反复失败再检查配置。",
            "warning",
        )
    else:
        flash(
            f"✅ 已自动回填上次填写的表单（共 {len([k for k,v in snapshot.items() if v])} 项）"
            "，请检查后点击「立即生成我的学习报告」重试。",
            "success",
        )

    # 关键改动：渲染 v3.5.2 表单页（而非首页 INDEX_HTML）→ 用户看到的是「已填好的表」
    # v3.9.12 · 401 不再 auto-focus 到 api_key（不假设是 Key 的问题）
    return render_template_string(
        GENERATE_FORM_HTML,
        form=snapshot,
        server_key_hint=_get_server_key_hint(),
        gesp_default_year=date.today().year,
        validation_result=None,
        focus_api_key=False,
    )


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


@app.route("/admin/reports/<path:filename>")
def admin_serve_report(filename):
    """v3.8 · admin 后台白名单：直接读 reports/ 下文件，跳过 _check_file_visibility 拦截。

    - 必须 admin 登录（否则跳 /admin/login）
    - 不走 hide_pdf / hide_html 拦截
    - 支持强制下载（as_attachment=True + download_name）
    """
    if not is_admin_authenticated():
        # 走普通拦截，避免暴露文件存在性
        from flask import redirect, url_for
        return redirect(url_for("admin_login", next=f"/admin/reports/{filename}"))
    # 防止路径穿越：拒绝 '..' / 绝对路径 / 跳到 reports 之外
    if ".." in filename or filename.startswith("/") or "\\" in filename:
        return ("Forbidden", 403)
    from pathlib import Path as _P
    reports_root = (_P(__file__).resolve().parent / "reports")
    if not reports_root.exists():
        reports_root = (_P.cwd() / "reports")
    target = (reports_root / filename).resolve()
    try:
        target.relative_to(reports_root.resolve())
    except ValueError:
        return ("Forbidden", 403)
    if not target.is_file():
        return ("Not Found", 404)
    # 推断下载名（保留原文件名）
    download_name = target.name
    return send_file(
        str(target),
        as_attachment=True,
        download_name=download_name,
        conditional=True,
    )


@app.route("/reports/<path:filename>")
def serve_report(filename):
    # v3.7 · 可见性拦截（PDF 默认隐藏，HTML 公开）
    rel = f"reports/{filename}"
    visible, reason = _check_file_visibility(rel)
    if not visible:
        return (
            f"""<!doctype html><html><head><meta charset="utf-8">
<title>暂未开放</title>
<style>body{{font-family:sans-serif;max-width:480px;margin:80px auto;
padding:0 20px;text-align:center;color:#444}}
h1{{font-size:18px;color:#dc2626}}
p{{font-size:14px;line-height:1.6}}</style></head>
<body><h1>🔒 {reason}</h1>
<p>如需查看报告内容，请通过家长分享的海报扫码进入在线版。</p>
<p><a href="/">← 返回首页</a></p>
</body></html>""",
            403,
            {"Content-Type": "text/html; charset=utf-8"},
        )

    report_root = (_ROOT / "reports").resolve()
    file_path = (report_root / filename).resolve()
    try:
        file_path.relative_to(report_root)
    except ValueError:
        return ("Forbidden", 403)

    if not file_path.is_file():
        return ("Not Found", 404)

    if file_path.suffix.lower() == ".pdf":
        from flask import send_file as _send_file
        response = _send_file(
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

    # v3.7 · 重建后继续维持 hide_pdf=1
    _record_hide_pdf(task_id)
    return html_url


ADMIN_LOGIN_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>管理员登录 - 洛谷 AI 测评报告</title>
    <script src="https://cdn.tailwindcss.com"></script>
    {{ app_skin_head() }}
</head>
<body class="app-body p-4 flex items-center justify-center">
    <div class="app-card max-w-md w-full">
        <div class="text-center mb-4">
            <div class="text-4xl mb-2">🔐</div>
            <h1 class="app-title">管理员登录</h1>
            <p class="app-subtitle">洛谷 AI 测评报告 · 后台管理</p>
        </div>
        {% if error %}
        <div class="app-box app-box-red mb-4">{{ error }}</div>
        {% endif %}
        {% if notice %}
        <div class="app-box app-box-green mb-4">{{ notice }}</div>
        {% endif %}
        <form method="post" action="/admin/login" class="space-y-4">
            <input type="hidden" name="next" value="{{ next_url }}">
            <div>
                <label class="app-label">管理员账号</label>
                <input type="text" name="username" value="{{ username }}" required class="app-input" autocomplete="username">
            </div>
            <div>
                <label class="app-label">管理员密码</label>
                <input type="password" name="password" required class="app-input" autocomplete="current-password">
            </div>
            <button type="submit" class="app-btn app-btn-primary">登录后台</button>
        </form>
        <p class="mt-4 text-xs text-gray-400 text-center">可通过环境变量 <code class="font-mono">ADMIN_USERNAME</code>、<code class="font-mono">ADMIN_PASSWORD</code>、<code class="font-mono">ADMIN_SESSION_SECRET</code> 配置管理员登录。</p>
        <p class="mt-3 text-center">
            <a href="/" class="text-xs text-emerald-700 hover:underline">← 返回首页</a>
        </p>
    </div>
</body>
</html>
"""


# ========== 后台管理页面 ==========

# v3.9 · 升学政策列表页（/admin/policies）
ADMIN_POLICIES_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>升学政策管理</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        .app-card { background: #fff; border-radius: 16px; box-shadow: 0 2px 12px rgba(0,0,0,0.06); }
        .app-title { font-size: 1.5rem; font-weight: 700; color: #1e293b; }
        .app-subtitle { color: #64748b; font-size: 0.875rem; }
        .app-link { color: #059669; text-decoration: none; padding: 0.25rem 0.5rem; border-radius: 4px; }
        .app-link:hover { background: #ecfdf5; }
        .app-pill { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 11px; font-weight: 600; }
        .app-pill-junior { background: #dbeafe; color: #1e40af; }
        .app-pill-senior { background: #fef3c7; color: #92400e; }
        .app-pill-university { background: #fce7f3; color: #9f1239; }
        .app-box { padding: 12px 16px; border-radius: 8px; margin-bottom: 16px; }
        .app-box-green { background: #ecfdf5; color: #065f46; border: 1px solid #6ee7b7; }
        .app-box-red { background: #fef2f2; color: #991b1b; border: 1px solid #fca5a5; }
    </style>
</head>
<body class="bg-slate-50 min-h-screen p-6">
    <div class="max-w-7xl mx-auto">
        <div class="flex items-center justify-between mb-6">
            <div>
                <h1 class="app-title">🏫 升学政策库</h1>
                <p class="app-subtitle">v3.9 · 家长报告第 3 章「升学政策窗口」自动引用 · 数据由 admin 维护</p>
                <p class="app-subtitle">📅 最近更新：{{ last_updated }} · 📊 总计 {{ total }} 所学校</p>
            </div>
            <div class="flex items-center gap-3 text-sm">
                <a href="/admin" class="app-link">← 返回后台</a>
                <a href="/admin/policies/new" class="bg-blue-600 text-white px-4 py-2 rounded-lg hover:bg-blue-700">+ 新增政策学校</a>
            </div>
        </div>

        {% if notice %}
        <div class="app-box {% if notice_type == 'error' %}app-box-red{% else %}app-box-green{% endif %}">{{ notice }}</div>
        {% endif %}

        {% for type_id, group in groups.items() %}
        <div class="app-card p-6 mb-6">
            <h2 class="text-lg font-bold text-gray-800 mb-3">
                {% if type_id == 'tech_talent_junior' %}🎒{% elif type_id == 'self_enroll_senior' %}📚{% else %}🎓{% endif %}
                {{ group.label }}（{{ group.rows|length }} 所）
            </h2>
            <div class="overflow-x-auto">
                <table class="w-full text-sm">
                    <thead>
                        <tr class="border-b-2 border-gray-200 text-gray-600">
                            <th class="text-left py-2">学校</th>
                            <th class="text-left py-2">城市/省份</th>
                            <th class="text-left py-2">学段</th>
                            <th class="text-left py-2">需竞赛奖项</th>
                            <th class="text-left py-2">政策摘要</th>
                            <th class="text-left py-2">招生</th>
                            <th class="text-left py-2">优先级</th>
                            <th class="text-left py-2">操作</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for p in group.rows %}
                        <tr class="border-b border-gray-100 hover:bg-gray-50">
                            <td class="py-2 font-semibold text-gray-900">{{ p.school_name }}</td>
                            <td class="py-2 text-gray-600">{{ p.city }} / {{ p.province }}</td>
                            <td class="py-2">
                                <span class="app-pill {% if p.target_stage == 'junior' %}app-pill-junior{% elif p.target_stage == 'senior' %}app-pill-senior{% else %}app-pill-university{% endif %}">
                                    {{ {'primary':'小学','junior':'初中','senior':'高中'}.get(p.target_stage, p.target_stage) }}
                                </span>
                            </td>
                            <td class="py-2 text-gray-700">{{ p.requires_competition or '—' }}</td>
                            <td class="py-2 text-gray-600 text-xs max-w-xs truncate" title="{{ p.policy_summary or '' }}">{{ p.policy_summary or '—' }}</td>
                            <td class="py-2 text-gray-600">{{ p.enrollment_count or '—' }}</td>
                            <td class="py-2 text-gray-600">{{ p.priority }}</td>
                            <td class="py-2 space-x-2">
                                <a href="/admin/policies/{{ p.id }}/edit" class="text-blue-600 hover:underline text-xs">编辑</a>
                                <form method="POST" action="/admin/policies/{{ p.id }}/delete" class="inline" onsubmit="return confirm('确认删除 {{ p.school_name }} 吗？');">
                                    <button type="submit" class="text-rose-600 hover:underline text-xs">🗑 删除</button>
                                </form>
                                {% if p.policy_url %}
                                <a href="{{ p.policy_url }}" target="_blank" class="text-emerald-600 hover:underline text-xs">📄 原文</a>
                                {% endif %}
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
        {% endfor %}

        {% if total == 0 %}
        <div class="app-card p-12 text-center text-gray-400">
            <p class="text-lg mb-2">📭 暂无政策学校</p>
            <p class="text-sm mb-4">点击右上角"新增政策学校"开始录入</p>
            <a href="/admin/policies/new" class="text-blue-600 hover:underline">立即添加</a>
        </div>
        {% endif %}
    </div>
</body>
</html>
"""

# v3.9 · 升学政策编辑/新增表单（/admin/policies/new, /admin/policies/<id>/edit）
ADMIN_POLICY_FORM_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>{% if policy.id %}编辑{% else %}新增{% endif %}政策学校</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        .app-card { background: #fff; border-radius: 16px; box-shadow: 0 2px 12px rgba(0,0,0,0.06); }
        .app-title { font-size: 1.5rem; font-weight: 700; color: #1e293b; }
        .app-link { color: #059669; text-decoration: none; }
        .form-label { display: block; font-size: 13px; font-weight: 600; color: #334155; margin-bottom: 4px; }
        .form-input, .form-select, .form-textarea {
            width: 100%; padding: 8px 12px; border: 1px solid #cbd5e1; border-radius: 8px;
            font-size: 14px; background: #fff;
        }
        .form-input:focus, .form-select:focus, .form-textarea:focus { outline: none; border-color: #3b82f6; }
        .form-help { font-size: 11px; color: #94a3b8; margin-top: 2px; }
        .btn-primary { background: #2563eb; color: #fff; padding: 10px 24px; border-radius: 8px; font-weight: 600; }
        .btn-primary:hover { background: #1d4ed8; }
        .btn-secondary { background: #f1f5f9; color: #334155; padding: 10px 24px; border-radius: 8px; }
        .btn-secondary:hover { background: #e2e8f0; }
        .app-box { padding: 12px 16px; border-radius: 8px; margin-bottom: 16px; }
        .app-box-red { background: #fef2f2; color: #991b1b; border: 1px solid #fca5a5; }
    </style>
</head>
<body class="bg-slate-50 min-h-screen p-6">
    <div class="max-w-3xl mx-auto">
        <div class="flex items-center justify-between mb-6">
            <h1 class="app-title">{% if policy.id %}✏️ 编辑政策学校{% else %}➕ 新增政策学校{% endif %}</h1>
            <a href="/admin/policies" class="app-link">← 返回列表</a>
        </div>

        {% if notice %}
        <div class="app-box app-box-red">{{ notice }}</div>
        {% endif %}

        <form method="POST" action="{{ action_url }}" class="app-card p-6 space-y-4">
            <div>
                <label class="form-label">学校名称 *</label>
                <input type="text" name="school_name" required maxlength="100"
                       value="{{ policy.school_name or '' }}"
                       class="form-input" placeholder="例：人大附中早培班" />
            </div>

            <div class="grid grid-cols-2 gap-4">
                <div>
                    <label class="form-label">学校类型 *</label>
                    <select name="school_type" required class="form-select">
                        {% for v, label in school_types %}
                        <option value="{{ v }}" {% if policy.school_type == v %}selected{% endif %}>{{ label }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div>
                    <label class="form-label">目标学段 *</label>
                    <select name="target_stage" required class="form-select">
                        {% for v, label in target_stages %}
                        <option value="{{ v }}" {% if policy.target_stage == v %}selected{% endif %}>{{ label }}</option>
                        {% endfor %}
                    </select>
                </div>
            </div>

            <div class="grid grid-cols-2 gap-4">
                <div>
                    <label class="form-label">城市 *</label>
                    <input type="text" name="city" required maxlength="20"
                           value="{{ policy.city or '全国' }}" class="form-input" placeholder="北京/全国" />
                </div>
                <div>
                    <label class="form-label">省份 *</label>
                    <input type="text" name="province" required maxlength="20"
                           value="{{ policy.province or '全国' }}" class="form-input" placeholder="北京/全国" />
                </div>
            </div>

            <div>
                <label class="form-label">需要的竞赛奖项</label>
                <input type="text" name="requires_competition" maxlength="100"
                       value="{{ policy.requires_competition or '' }}"
                       class="form-input" placeholder="例：CSP-J 一等 / GESP 7级 80+" />
                <p class="form-help">家长报告里会逐条引用</p>
            </div>

            <div>
                <label class="form-label">政策摘要</label>
                <textarea name="policy_summary" rows="3" maxlength="500"
                          class="form-textarea" placeholder="例：信息学省一 30 分加分 / 面试 30% + 笔试 70%">{{ policy.policy_summary or '' }}</textarea>
            </div>

            <div class="grid grid-cols-3 gap-4">
                <div>
                    <label class="form-label">招生人数</label>
                    <input type="number" name="enrollment_count" min="0" max="9999"
                           value="{{ policy.enrollment_count or '' }}" class="form-input" />
                </div>
                <div>
                    <label class="form-label">优先级 (越小越前)</label>
                    <input type="number" name="priority" min="1" max="999"
                           value="{{ policy.priority or 100 }}" class="form-input" />
                </div>
                <div>
                    <label class="form-label">生效年份</label>
                    <input type="number" name="effective_year" min="2020" max="2099"
                           value="{{ policy.effective_year or 2026 }}" class="form-input" />
                </div>
            </div>

            <div>
                <label class="form-label">政策原文链接</label>
                <input type="url" name="policy_url" maxlength="500"
                       value="{{ policy.policy_url or '' }}"
                       class="form-input" placeholder="https://..." />
            </div>

            <div class="flex items-center gap-3 pt-4 border-t">
                <button type="submit" class="btn-primary">💾 保存</button>
                <a href="/admin/policies" class="btn-secondary">取消</a>
            </div>
        </form>
    </div>
</body>
</html>
"""
ADMIN_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>后台管理 - 洛谷 AI 测评报告</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <meta http-equiv="refresh" content="10">
    {{ app_skin_head() }}
</head>
<body class="app-body p-6">
    <div class="max-w-6xl mx-auto space-y-4">
        <div class="app-card flex items-center justify-between">
            <div>
                <h1 class="app-title">🛠 后台管理</h1>
                <p class="app-subtitle">洛谷 AI 测评报告 · 任务总览</p>
            </div>
            <div class="flex items-center gap-4 text-sm">
                <span class="text-gray-500">管理员：<span class="font-semibold text-emerald-700">{{ admin_user }}</span></span>
                <a href="/admin/students" class="app-link">学员档案</a>
                <a href="/admin/codes" class="app-link">兑换码</a>
                <a href="/admin/codes?sku=parent_invite" class="app-link text-amber-700 font-semibold">🔑 邀请码管理</a>
                <a href="/admin/policies" class="app-link text-blue-700 font-semibold">🏫 升学政策</a>
                <a href="/admin/schools" class="app-link text-purple-700 font-semibold">🎓 校徽管理</a>
                <a href="/" class="app-link">返回首页</a>
                <a href="/admin/logout" class="text-red-600 hover:underline">退出登录</a>
            </div>
        </div>

        {% if notice %}
        <div class="app-box {% if notice_type == 'error' %}app-box-red{% else %}app-box-green{% endif %}">{{ notice }}</div>
        {% endif %}

        <!-- 统计卡片 -->
        <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div class="app-card">
                <p class="text-sm text-gray-500">总生成次数</p>
                <p class="text-2xl font-extrabold text-emerald-700">{{ total_tasks }}</p>
            </div>
            <div class="app-card">
                <p class="text-sm text-gray-500">今日生成</p>
                <p class="text-2xl font-extrabold text-emerald-600">{{ today_tasks }}</p>
            </div>
            <div class="app-card">
                <p class="text-sm text-gray-500">进行中</p>
                <p class="text-2xl font-extrabold text-amber-600">{{ running_tasks }}</p>
            </div>
            <div class="app-card">
                <p class="text-sm text-gray-500">失败次数</p>
                <p class="text-2xl font-extrabold text-rose-600">{{ error_tasks }}</p>
            </div>
        </div>

        <!-- v3.9 · 历史任务列表（按 UID 折叠分组） -->
        <div class="app-card p-0 overflow-hidden">
            <div class="px-6 py-4 border-b border-emerald-100 flex items-center justify-between">
                <h2 class="text-lg font-bold text-emerald-900">📋 历史任务列表（按 UID 折叠）</h2>
                <span class="text-xs text-gray-500">共 {{ task_groups|length }} 个 UID</span>
            </div>
            <div class="divide-y divide-emerald-50">
                {% for group in task_groups %}
                <details class="group" {% if loop.first %}open{% endif %}>
                    <summary class="px-6 py-3 cursor-pointer hover:bg-emerald-50 flex items-center gap-3 select-none">
                        <span class="text-emerald-700 group-open:rotate-90 transition-transform inline-block">▶</span>
                        {% if group.luogu_uid %}
                        <span class="font-mono text-xs font-bold text-amber-700 bg-amber-50 px-2 py-0.5 rounded">UID {{ group.luogu_uid }}</span>
                        {% else %}
                        <span class="text-xs font-bold text-rose-700 bg-rose-50 px-2 py-0.5 rounded">⚠️ 无 UID（孤儿报告）</span>
                        {% endif %}
                        <span class="font-semibold text-gray-900">{{ group.name }}</span>
                        <span class="text-xs text-gray-500">{{ group.school }} · {{ group.grade }}</span>
                        <span class="ml-auto flex items-center gap-3 text-xs text-gray-500">
                            <span class="text-emerald-700 font-bold">{{ group.task_count }} 次报告</span>
                            <span>最近：{{ group.latest_time }}</span>
                        </span>
                    </summary>
                    <div class="overflow-x-auto bg-gray-50/50">
                        <table class="app-table">
                            <thead>
                                <tr class="bg-gray-100/70">
                                    <th>任务 ID</th>
                                    <th>通过</th>
                                    <th>失败</th>
                                    <th>状态</th>
                                    <th>时间</th>
                                    <th>操作</th>
                                </tr>
                            </thead>
                            <tbody>
                                {% for task in group.tasks %}
                                <tr class="hover:bg-emerald-50/50">
                                    <td class="font-mono text-xs text-gray-500">{{ task.id[:8] }}...</td>
                                    <td class="text-emerald-700 font-semibold">{{ task.solved }}</td>
                                    <td class="text-rose-600 font-semibold">{{ task.failed }}</td>
                                    <td>
                                        {% if task.status == 'done' %}
                                            <span class="app-pill app-pill-done">完成</span>
                                        {% elif task.status == 'error' %}
                                            <span class="app-pill app-pill-error">失败</span>
                                        {% elif task.status == 'running' %}
                                            <span class="app-pill app-pill-running">进行中</span>
                                        {% else %}
                                            <span class="app-pill app-pill-muted">{{ task.status }}</span>
                                        {% endif %}
                                    </td>
                                    <td class="text-xs text-gray-500">{{ task.time }}</td>
                                    <td class="space-y-1">
                                        {% if task.html %}
                                        <a href="{{ task.html }}" target="_blank" class="text-emerald-700 hover:underline text-xs font-semibold">HTML</a>
                                        {% endif %}
                                        {% if task.pdf %}
                                        {# v3.8 · admin 后台可下载 PDF（用户态报告页已隐藏，统一走海报） #}
                                        <a href="/admin/reports/{{ task.pdf | replace('reports/', '') }}" download class="text-emerald-700 hover:underline text-xs font-semibold ml-1" title="v3.8 仅管理员可下载 PDF">PDF</a>
                                        {% endif %}
                                        {% if task.md %}
                                        <a href="{{ task.md }}" target="_blank" class="text-emerald-700 hover:underline text-xs font-semibold ml-1">MD</a>
                                        {% endif %}
                                        {% if task.can_rebuild %}
                                        <form method="post" action="/admin/rebuild-html/{{ task.id }}" class="inline-block ml-1">
                                            <button type="submit" class="text-xs text-emerald-700 hover:underline font-semibold">重建 HTML</button>
                                        </form>
                                        {% endif %}
                                        {# v3.8 · admin 强制删除（DB + 磁盘文件，绕过 24h 限流）#}
                                        {% if task.id and not task.is_orphan %}
                                        <form method="post" action="/admin/reports/{{ task.id }}/delete" class="inline-block ml-1"
                                              onsubmit="return confirm('确认删除任务 {{ task.id[:8] }}... 吗？\\n\\n将删除数据库记录和报告文件，该学员 24h 限流会解除。\\n（可重新生成报告）')">
                                            <button type="submit" class="text-xs text-rose-600 hover:text-rose-800 hover:underline font-semibold" title="v3.8 强制删除报告，绕过 24h 限流">🗑 删除</button>
                                        </form>
                                        {% endif %}
                                        {% if task.rebuild_status == 'running' %}
                                        <div class="text-xs text-amber-600">重建中...</div>
                                        {% elif task.rebuild_status == 'done' %}
                                        <div class="text-xs text-emerald-600">已重建</div>
                                        {% elif task.rebuild_status == 'error' %}
                                        <div class="text-xs text-rose-600">重建失败</div>
                                        {% endif %}
                                        {% if task.rebuild_message %}
                                        <div class="text-[11px] text-gray-500">{{ task.rebuild_message }}</div>
                                        {% endif %}
                                    </td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                </details>
                {% endfor %}
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


def _auto_upsert_student_profile(luogu_uid: str, form: dict) -> None:
    """v3.8 · 自动补全学员档案

    解决"明明填写了城市，海报/AI 报告却说缺少城市/学校"的问题：
      - 如**果**学**员**档**案**不**存**在** → **自**动**创**建**（**从** form 取**姓**名**/**城**市**/**省**份**/**学**校**/**年**级**）
      - 如**果**学**员**档**案**存**在**但** city/school/province **为**空** → **用** form 中**的**信**息** UPDATE

    不打**断** run_generation 主**流**程**。
    """
    if not str(luogu_uid or "").strip():
        return
    try:
        existing = _admin_students.get_student_by_uid(luogu_uid)
        # 从 form 提取字段（统一 strip）
        name = (form.get("student_name") or "").strip()
        city = (form.get("city") or "").strip()
        province = (form.get("province") or "").strip()
        school = (form.get("school") or "").strip()
        grade = (form.get("grade") or "").strip()
        gender = (form.get("gender") or "").strip().upper()
        if gender and gender not in ("M", "F"):
            gender = ""

        if not existing:
            # 学员档案不存在 → 创建（必须有姓名 + UID 才有意义）
            if not name:
                return
            try:
                _admin_students.create_student(
                    luogu_uid=str(luogu_uid).strip(),
                    real_name=name or None,
                    school=school or None,
                    grade=grade or None,
                    city=city or None,
                    province=province or None,
                    gender=gender or None,
                    is_minor=False,  # 兜底用 False，由家长后续补未成年人标记
                    registered_via="auto_from_report",
                )
                app.logger.info(
                    f"v3.8 自动创建学员档案 UID={luogu_uid} (name={name}, city={city}, school={school})"
                )
                return
            except Exception as _ce:
                app.logger.warning(f"v3.8 自动创建学员档案失败 UID={luogu_uid}: {_ce}")
                return

        # 档案存在 → 检查空字段并 UPDATE
        updates: dict[str, str] = {}
        if not (existing.get("city") or "").strip() and city:
            updates["city"] = city
        if not (existing.get("school") or "").strip() and school:
            updates["school"] = school
        if not (existing.get("province") or "").strip() and province:
            updates["province"] = province
        if not (existing.get("grade") or "").strip() and grade:
            updates["grade"] = grade
        if not (existing.get("real_name") or "").strip() and name:
            updates["real_name"] = name
        if not (existing.get("gender") or "").strip() and gender in ("M", "F"):
            updates["gender"] = gender

        if not updates:
            return

        from task_store import _get_conn
        conn = _get_conn()
        try:
            set_clauses = ", ".join(f"{k} = ?" for k in updates.keys())
            values = list(updates.values()) + [int(existing["id"])]
            conn.execute(f"UPDATE students SET {set_clauses} WHERE id = ?", values)
            conn.commit()
            app.logger.info(
                f"v3.8 自动补全学员档案 UID={luogu_uid} (sid={existing['id']}): 补全字段 {list(updates.keys())}"
            )
            # v3.9.18 · 档案补全后，失效 parent_subscribe.html/.md 缓存，
            # 避免之前 AI 生成的「未填城市」陈旧内容误导家长。
            try:
                _latest_dir = _find_latest_report_dir(luogu_uid, (existing.get("real_name") or "").strip())
                if _latest_dir:
                    for _fn in ("parent_subscribe.html", "parent_subscribe.md"):
                        _fp = _latest_dir / _fn
                        if _fp.exists():
                            _fp.unlink()
            except Exception:
                pass
        except Exception as _ue:
            app.logger.warning(f"v3.8 自动补全学员档案失败 UID={luogu_uid}: {_ue}")
        finally:
            conn.close()
    except Exception as _e:
        app.logger.warning(f"v3.8 _auto_upsert_student_profile 异常 UID={luogu_uid}: {_e}")


def _purge_report_files_from_task(task: dict) -> int:
    """v3.8 · 物理删除一条任务关联的 html / pdf / md 文件，返回实际删除的文件数

    Args:
        task: task_store.get_task 返回的字典（含 html/pdf/md 字段 · 形如 /reports/xxx/yyy.html?v=1）

    安全：
        - 仅删除项目根 / reports 目录下的文件
        - 自动剥除 /reports/ 前缀 + ?v=xxx 后缀
        - 缺失/越界文件跳过
    """
    if not task:
        return 0
    reports_root = (_ROOT / "reports")
    if not reports_root.exists():
        return 0
    deleted = 0
    for key in ("html", "pdf", "md"):
        raw = str(task.get(key) or "").strip()
        if not raw or not raw.startswith("/"):
            continue
        # 去掉 ?v=xxx
        rel = raw.split("?", 1)[0].lstrip("/")
        # 去掉 /reports/ 前缀（task 字段存的是 /reports/xxx/yyy.html）
        if rel.startswith("reports/"):
            rel = rel[len("reports/"):]
        if not rel or ".." in rel:
            continue
        try:
            target = (reports_root / rel).resolve()
            target.relative_to(reports_root.resolve())  # 防越界
        except Exception:
            continue
        try:
            if target.is_file():
                target.unlink()
                deleted += 1
        except Exception as _e:
            app.logger.warning(f"删除报告文件失败 {target}: {_e}")
    return deleted


@app.route("/admin/reports/<task_id>/delete", methods=["POST"])
def admin_delete_report(task_id: str):
    """v3.8 · admin 强制删除一条历史报告（DB + 磁盘文件），用于绕过 24h 限流或清理脏数据"""
    auth_redirect = require_admin_auth()
    if auth_redirect is not None:
        return auth_redirect
    task_id = str(task_id or "").strip()
    if not task_id:
        return redirect(url_for("admin_page", notice="任务 ID 缺失", notice_type="error"))
    # 1. 读取任务（拿到 html/pdf/md 路径）
    try:
        from task_store import get_task as _get_task
        task = _get_task(task_id)
    except Exception as _e:
        app.logger.warning(f"admin_delete_report · get_task 失败: {_e}")
        task = None
    # 2. 删磁盘文件
    files_deleted = _purge_report_files_from_task(task or {})
    # 3. 删 DB
    try:
        from task_store import delete_task as _delete_task
        db_deleted = _delete_task(task_id)
    except Exception as _e:
        app.logger.error(f"admin_delete_report · delete_task 失败: {_e}")
        return redirect(url_for("admin_page", notice=f"DB 删除失败: {_e}", notice_type="error"))
    if not db_deleted and not files_deleted:
        return redirect(url_for("admin_page", notice=f"任务 {task_id} 不存在", notice_type="error"))
    msg = f"已删除任务 {task_id[:8]}...（DB 1 条 + 磁盘 {files_deleted} 个文件）。该学员 24h 限流已解除。"
    return redirect(url_for("admin_page", notice=msg, notice_type="success"))


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
            "luogu_uid": str(row.get("luogu_uid", "") or ""),  # v3.9 · 按 UID 分组用
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
    # v3.9 · orphan 任务的 luogu_uid 字段已由 discover_orphan_report_tasks
    #   通过 students 表反查填充（.get("luogu_uid", "")），不要再次覆盖为空。
    task_list.extend(orphan_tasks)
    task_list.sort(key=lambda task: task.get("sort_time", datetime.min), reverse=True)

    # v3.9 · 按 UID 分组折叠（同一个 UID 的多次任务合并到一个 <details> 面板）
    #   v3.9.1 修复：组内最新时间倒序、且**有 UID 的排在孤儿前面**
    #   元组排序：key=(has_orphan, latest_time)，has_orphan=False 排前
    #   但 latest_time 在 has_orphan 内部要倒序（最新在前），所以两层 key：
    #     primary key: has_orphan（False=0 < True=1 排前）
    #     secondary key: latest_time 倒序（用 -ord 模式实现）
    from collections import OrderedDict
    grouped: "OrderedDict[str, dict]" = OrderedDict()
    for task in task_list:
        key = task.get("luogu_uid") or f"__orphan_{task.get('id', '')}"
        # 用"任务 ID 前 8 位"作为 orphan 的 key，避免合并不同 orphan
        if key not in grouped:
            grouped[key] = {
                "luogu_uid": task.get("luogu_uid", ""),
                "name": task.get("name", "未知"),
                "school": task.get("school", "未知"),
                "grade": task.get("grade", "未知"),
                "task_count": 0,
                "latest_time": "",
                "status_summary": "",
                "tasks": [],
            }
        g = grouped[key]
        g["task_count"] += 1
        g["tasks"].append(task)
        # 更新组内最新时间
        t_str = str(task.get("time", ""))
        if t_str > (g["latest_time"] or ""):
            g["latest_time"] = t_str
            g["name"] = task.get("name", g["name"])  # 用最新任务的姓名
    # 按 (has_orphan, latest_time) 排序：
    #   1) has_orphan = (luogu_uid == "")  → False(有 UID) 排前
    #   2) 同一档内：最新任务时间大的排前（字符串字典序倒序）
    #   v3.9.1 修：sort 接受 reverse 单参数，无法同时让 has_orphan 升 + latest_time 降，
    #              改用两次 stable sort（Python 3 sorted 是稳定的）：
    #              - 先按 latest_time 倒序
    #              - 再按 has_orphan 升序（False 排前，孤儿排后）
    #              第二次排序会保留第一次的相对顺序
    intermediate = sorted(
        grouped.values(),
        key=lambda g: g["latest_time"],
        reverse=True,  # 最新时间排前
    )
    sorted_groups = sorted(
        intermediate,
        key=lambda g: g["luogu_uid"] == "",  # False(有 UID) 排前，True(孤儿) 排后
    )
    # 同一组内任务按时间倒序
    for g in sorted_groups:
        g["tasks"].sort(key=lambda t: str(t.get("time", "")), reverse=True)

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
        task_groups=sorted_groups,  # v3.9 · 按 UID 分组后传给模板
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
                city=str(request.form.get("city", "") or "").strip() or None,  # v3.8 · 城市
                province=str(request.form.get("province", "") or "").strip() or None,  # v3.8 · 省份
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
    luogu_uid = str(student.get("luogu_uid") or "").strip()
    student_name = (student.get("real_name") or "").strip()
    progress = _admin_students.get_student_gesp_progress(sid) or {}
    # v3.8 · 错题本：优先从最新 AI 报告的 export.json.failed_items 抽取（未通过题目 + 标签）
    mistakes: list[dict] = []
    try:
        latest = _find_latest_report_dir(luogu_uid, student_name)
        if latest:
            export_json = latest / "export.json"
            if export_json.exists():
                import json as _json
                _exp = _json.loads(export_json.read_text(encoding="utf-8", errors="replace"))
                for fi in (_exp.get("failed_items") or []):
                    p = fi.get("problem") or {}
                    if not isinstance(p, dict):
                        continue
                    pid = (p.get("pid") or "").strip()
                    if not pid:
                        continue
                    # tags 可能是 tag_id 列表，尝试通过 tag_by_id 查名字（若有）
                    tag_name = ""
                    tag_ids = p.get("tags") or []
                    if isinstance(tag_ids, list) and tag_ids:
                        # 第一个 tag 作为主分类
                        first = tag_ids[0]
                        tag_name = str(first) if isinstance(first, str) else ""
                    mistakes.append({
                        "problem_id": pid,
                        "pid": pid,
                        "title": p.get("title") or "未命名题目",
                        "tag": tag_name or "未分类",
                        "difficulty": p.get("difficulty"),
                    })
    except Exception as _e:
        app.logger.warning(f"_collect_report_data 抽取 failed_items 失败: {_e}")
    # 兜底：若 AI 报告没抽到错题，回退到 mistake_book（保持旧逻辑可用）
    if not mistakes:
        try:
            from mistake_book import list_mistakes
            mistakes = list_mistakes(sid) or []
        except Exception:
            mistakes = []
    mistake_count = len(mistakes)
    # 政策匹配
    try:
        from task_store import match_school_for_student
        policy_match = match_school_for_student(dict(student))
    except Exception:
        policy_match = {"stage": "unknown", "matches": []}
    # 年龄 & 免初赛
    try:
        from docs.gesp_estimator import is_csp_age_eligible, compute_exemptions
    except Exception:
        from gesp_estimator import is_csp_age_eligible, compute_exemptions
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


def _list_student_report_htmls(luogu_uid: str, student_name: str = "", limit: int = 10) -> list[dict]:
    """v3.8 · 列出学员最近 N 份 HTML 报告（按 mtime 倒序）

    返回：[{dir_name, html_url, mtime_display, share_url, has_poster, size_kb, status}, ...]

    v3.9.17 · 不再只列有 report.html 的 dir：有 export_data.json 的也算"数据已抓取"
    （AI 报告生成失败时 export_data.json 仍存在，只是 report.md 是 0 字节）。
    这些"半完成"状态对学员仍有价值：能看到 6 维评分、抓题数、难度分布等。
    """
    items: list[dict] = []
    try:
        reports_root = (ROOT / "reports") if (ROOT / "reports").exists() else (ROOT / "data" / "reports")
        if not reports_root.exists():
            return []
        # 报告目录命名规则：<name>_<uid>_<YYYYMMDD-HHMMSS>
        for d in reports_root.iterdir():
            if not d.is_dir():
                continue
            # v3.9.17 · 改为：要求 export_data.json 存在（不再要求 report.html）
            # 这能让"AI 报告未生成"的 dir 也显示在历史里
            export_p = d / "export_data.json"
            html_p = d / "report.html"
            if not export_p.exists():
                continue
            dir_name = d.name
            # 校验目录与该 uid 相关（v3.9.17 · 优先侧车 luogu_uid.txt 精确匹配，回退到目录名包含）
            _matches = False
            if luogu_uid:
                # 1) 侧车文件精确匹配（最可靠）
                _sidecar = d / "luogu_uid.txt"
                if _sidecar.exists():
                    try:
                        if _sidecar.read_text(encoding="utf-8", errors="replace").strip() == str(luogu_uid).strip():
                            _matches = True
                    except Exception:
                        pass
                # 2) 旧式：目录名包含 luogu_uid
                if not _matches and str(luogu_uid) in dir_name:
                    _matches = True
            else:
                _matches = True
            if not _matches:
                continue
            # v3.9.17 · 状态分三种：
            #   - "complete": report.html 存在且非 0 字节
            #   - "data_only": 只有 export_data.json（AI 报告未生成）
            #   - "broken": 都没
            html_size = html_p.stat().st_size if html_p.exists() else 0
            report_md_size = (d / "report.md").stat().st_size if (d / "report.md").exists() else 0
            if html_size > 1024:  # > 1KB 才算完整
                status = "complete"
            elif export_p.exists() and export_p.stat().st_size > 1024:
                if report_md_size > 100:
                    status = "data_only"  # AI 部分输出但没生成 HTML
                else:
                    status = "data_only"  # 数据齐了但 AI 失败
            else:
                continue
            # 优先用 report.html mtime，否则 export_data.json
            ref_p = html_p if status == "complete" else export_p
            stat = ref_p.stat()
            # v3.9.38 · 显式转北京时间（之前用 datetime.fromtimestamp() 是 UTC 偏 8h）
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=_BJ_TZ)
            items.append({
                "dir_name": dir_name,
                "html_url": f"/reports/{dir_name}/report.html" if status == "complete" else "",
                "mtime_display": mtime.strftime("%Y-%m-%d %H:%M"),
                "share_url": f"/me/{luogu_uid}/share-card.png",
                "has_poster": (d / "share-card.png").exists(),
                "size_kb": round(stat.st_size / 1024, 1),
                "status": status,  # v3.9.17
            })
        items.sort(key=lambda x: x["mtime_display"], reverse=True)
        return items[:limit]
    except Exception as _e:
        app.logger.warning(f"_list_student_report_htmls failed: {_e}")
        return []


@app.route("/report/student/<luogu_uid>")
def report_student(luogu_uid: str):
    """v3.9.7 · 学员版报告已合并到个人中心 → 统一跳转 /me/<uid>（保留旧链接以免外部引用 404）"""
    return redirect(url_for("student_me", luogu_uid=luogu_uid), code=301)


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
    {{ app_skin_head() }}
    <style>
        .progress-fill{transition:width 1s ease;}
        .medal{font-size:36px;display:inline-block;filter:drop-shadow(0 2px 4px rgba(0,0,0,.1));}
    </style>
</head>
<body class="app-body p-4">
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
                    <a id="shareCardDownload" href="/me/{{ luogu_uid }}/share-card.png" download="我家孩子位置图_{{ student.real_name or luogu_uid }}.png"
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

    <!-- v3.8 · 历史报告 HTML（直接展示，📤 图标触发分享模态框） -->
    <div class="bg-white rounded-2xl card-shadow p-5">
        <div class="flex items-center justify-between mb-3">
            <h2 class="text-base font-bold text-gray-800">📄 历史报告（HTML）</h2>
            <span class="text-xs text-gray-400">共 {{ report_htmls|length }} 份</span>
        </div>
        {% if report_htmls %}
        <div class="space-y-2">
            {% for r in report_htmls %}
            <div class="flex items-center justify-between border border-gray-200 rounded-lg p-3 hover:bg-gray-50">
                <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-2">
                        <span class="text-sm font-bold text-emerald-700">📅 {{ r.mtime_display }}</span>
                        {% if loop.first %}<span class="text-[10px] px-1.5 py-0.5 bg-emerald-100 text-emerald-700 rounded">最新</span>{% endif %}
                        {% if r.has_poster %}<span class="text-[10px] px-1.5 py-0.5 bg-rose-100 text-rose-700 rounded">海报已生成</span>{% endif %}
                    </div>
                    <div class="text-[11px] text-gray-400 mt-0.5 truncate">{{ r.dir_name }} · {{ r.size_kb }} KB</div>
                </div>
                <div class="flex items-center gap-1.5 ml-2">
                    <a href="{{ r.html_url }}" target="_blank"
                       class="px-2.5 py-1.5 rounded-md bg-emerald-50 hover:bg-emerald-100 text-emerald-700 text-xs font-bold">🔍 查看</a>
                    <button type="button" onclick="openSharePosterByUrl('{{ r.share_url }}', '{{ r.dir_name }}')"
                            class="w-8 h-8 rounded-full bg-gradient-to-br from-rose-500 to-pink-500 text-white text-sm shadow hover:shadow-md hover:scale-105 transition flex items-center justify-center"
                            title="分享此版本报告（生成海报）">📤</button>
                </div>
            </div>
            {% endfor %}
        </div>
        <p class="text-[10px] text-gray-400 mt-3">💡 点击 📤 重新生成该版本的海报（5-15 秒，生成后自动下载）</p>
        {% else %}
        <div class="text-center py-4 text-sm text-gray-400">🌱 暂无历史报告</div>
        {% endif %}
    </div>

    <!-- 错题本卡片（游戏化） -->
    <div class="bg-white rounded-2xl card-shadow p-5">
        <div class="flex items-center justify-between mb-3">
            <h2 class="text-base font-bold text-gray-800">📚 我的错题本</h2>
            <span class="text-xs text-gray-400">{{ mistake_count }} 道错题</span>
        </div>
        {% if mistakes %}
        <div class="space-y-2">
            {% for m in mistakes[:8] %}
            <div class="flex items-center justify-between border border-gray-200 rounded-lg p-2.5 hover:bg-gray-50">
                <div class="text-sm flex-1 min-w-0">
                    <div class="flex items-center gap-2">
                        <span class="font-mono text-xs text-gray-400">{{ m.problem_id or m.pid or '—' }}</span>
                        <span class="truncate">{{ m.title or m.problem_title or '未命名题目' }}</span>
                    </div>
                    <div class="flex items-center gap-2 mt-0.5">
                        <span class="text-[10px] px-1.5 py-0.5 bg-amber-100 text-amber-700 rounded">{{ m.tag or m.algorithm_tag or '未分类' }}</span>
                        {% if m.difficulty %}<span class="text-[10px] text-gray-400">难度 {{ m.difficulty }}</span>{% endif %}
                    </div>
                </div>
                <!-- v3.9.9 · 每题独立 AI 讲题入口（直跳 aijiangti.cn，题目已直传 + C++ 实现要求） -->
                <a href="https://aijiangti.cn/?pid={{ m.problem_id or m.pid }}&from=luogu&lang=cpp&require={{ '用C++代码实现并讲解'|urlencode }}&source={{ (m.source or '')|urlencode }}&title={{ (m.title or '')|urlencode }}"
                   target="_blank" rel="noopener"
                   class="ml-2 px-2.5 py-1.5 rounded-md bg-gradient-to-r from-blue-500 to-cyan-500 text-white text-xs font-bold hover:from-blue-600 hover:to-cyan-600 whitespace-nowrap"
                   title="跳到 aijiangti.cn 生成 C++ 课件（题号/标题/来源已传入）">
                    🤖 AI 讲题
                </a>
            </div>
            {% endfor %}
            {% if mistakes|length > 8 %}
            <div class="text-center text-xs text-gray-400 pt-1">…还有 {{ mistakes|length - 8 }} 道</div>
            {% endif %}
        </div>
        <p class="text-[10px] text-gray-400 mt-2">💡 数据来源：最新 AI 报告 · export.json 的未通过题目</p>
        {% else %}
        <div class="text-center py-4 text-sm text-gray-400">🎉 太棒了 · 暂无未通过题目</div>
        {% endif %}
        <!-- AI 讲题批量入口（家长订阅门控 · 走 StudyMate 批量讲解） -->
        <div class="mt-3 pt-3 border-t border-gray-100">
            {% if has_parent_sub %}
            <a href="/studymate/dashboard" class="block w-full text-center py-2.5 rounded-lg bg-gradient-to-r from-blue-500 to-cyan-500 text-white font-bold text-sm hover:from-blue-600 hover:to-cyan-600">🤖 一键 AI 讲题（StudyMate 批量）</a>
            {% else %}
            <button disabled class="w-full py-2.5 rounded-lg bg-gray-100 text-gray-400 font-bold text-sm cursor-not-allowed">🤖 批量 AI 讲题 🔒（需家长订阅）</button>
            <p class="text-center text-xs text-amber-600 mt-1">💡 单题 AI 讲题免费 · 批量讲题需家长加 V 兑换码 <code class="bg-amber-50 px-1 rounded">PS-XXXXXXXX</code> 解锁</p>
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
<script>
// v3.8 · 历史报告行的 📤 分享按钮：复用 shareModal，注入指定版本的 share-card.png
function openSharePosterByUrl(shareUrl, dirName) {
    var m = document.getElementById('shareModal');
    var img = document.getElementById('shareCardImg');
    if (!m || !img) return;
    var url = shareUrl + '?v=' + encodeURIComponent(dirName) + '&t=' + Date.now();
    img.src = url;
    img.style.display = '';
    img.alt = '分享海报 · ' + dirName;
    m.classList.remove('hidden');
    // 修改下载按钮
    var dl = document.getElementById('shareCardDownload');
    if (dl) {
        dl.setAttribute('href', url);
        dl.setAttribute('download', '分享海报_' + dirName + '.png');
    }
}
</script>
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
    {{ app_skin_head() }}
    <style>
        .card-shadow{box-shadow:0 10px 25px rgba(0,0,0,.06);}
    </style>
</head>
<body class="app-body p-4">
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
    {{ app_skin_head() }}
    <style>
        .card-shadow{box-shadow:0 10px 25px rgba(0,0,0,.06);}
        .gradient-text{background:linear-gradient(90deg,#f59e0b,#ec4899);-webkit-background-clip:text;background-clip:text;color:transparent;}
        .countdown-pill{display:inline-block;padding:2px 8px;border-radius:9999px;font-size:11px;font-weight:600;}
        .copy-btn{cursor:pointer;transition:all .2s;}
        .copy-btn:hover{background:#f3f4f6;}
    </style>
</head>
<body class="app-body p-4">
<div class="max-w-4xl mx-auto py-6 space-y-4">

    <!-- 头部：付费版品牌 + 学员信息 -->
    <div class="bg-white rounded-2xl card-shadow p-6">
        <div class="flex items-start justify-between flex-wrap gap-3">
            <div>
                <div class="flex items-center gap-2">
                    <span class="text-xs px-2 py-0.5 bg-amber-100 text-amber-700 rounded-full">
                        📨 家长订阅版{% if not commerce_hidden %} · ¥30/月{% endif %}
                    </span>
                    <span class="text-xs px-2 py-0.5 bg-rose-100 text-rose-700 rounded-full">v3.9 · 5 维度深度</span>
                </div>
                <h1 class="text-2xl font-extrabold text-gray-800 mt-2">
                    {{ student.real_name or '您家孩子' }} 的 OI 决策报告
                </h1>
                <p class="text-xs text-gray-500 mt-1">
                    {{ student.city or '所在城市待补' }}{% if student.province %} · {{ student.province }}{% endif %}
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

    <!-- 触发 AI 决策支持生成表单（仅当已生成基础报告、且还没有家长订阅版时显示） -->
    {% if has_report %}
    <div class="bg-gradient-to-r from-amber-50 to-rose-50 rounded-2xl card-shadow p-6 border-2 border-amber-200">
        <h2 class="text-lg font-bold text-gray-800">🚀 触发生成 AI 家长决策支持报告</h2>
        <p class="text-xs text-gray-600 mt-1">基于同账号的报告 {{ report_dir_name }} 让 AI 写一份"家长视角"的决策支持分析（约 1-2 分钟）</p>
        {% if error_msg %}
        <div class="mt-3 bg-rose-100 border border-rose-300 text-rose-800 text-sm p-3 rounded">❌ {{ error_msg }}</div>
        {% endif %}
        <form method="POST" action="/me/{{ luogu_uid }}/start-parent-subscribe" class="mt-4 space-y-3">
            {% if not commerce_hidden %}
            {# v3.9 · 传播期隐藏 API Key / 模型配置（家长无需关心技术细节）#}
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
            {% endif %}
            <button type="submit" class="w-full bg-gradient-to-r from-amber-500 to-rose-500 hover:from-amber-600 hover:to-rose-600 text-white font-bold py-3 rounded-md transition">
                📨 开始 AI 决策支持生成 · 约 1-2 分钟
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
            v3.9 · 家长深度报告 · AI 估算水印 · 数据更新于 {{ student.updated_at or '—' }}
        </p>
        {% if not commerce_hidden %}
        <p class="text-center text-xs text-gray-400 mt-2">
            💎 订阅状态：<span class="font-bold {% if has_parent_sub %}text-emerald-600{% else %}text-rose-600{% endif %}">
                {% if has_parent_sub %}已订阅（有效期内）{% else %}未订阅 · <a href="/redeem" class="underline">激活订阅</a>{% endif %}
            </span>
        </p>
        {% endif %}
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
    {{ app_skin_head() }}
    <style>
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
<body class="app-body">
<div class="container">
    <div class="card">
        <div class="flex items-center justify-between flex-wrap gap-3">
            <div>
                <span class="inline-block text-xs px-2 py-0.5 bg-amber-100 text-amber-700 rounded-full">📨 家长专属深度报告</span>
                <h1 class="text-2xl font-extrabold text-gray-800 mt-2">您家孩子 {{ student_name }} 的 OI 学习深度分析</h1>
                <p class="text-xs text-gray-500 mt-1">UID {{ luogu_uid }} · 生成于 {{ generated_at }}</p>
            </div>
            <div class="flex gap-2">
                {# v3.9.18 · 只保留「学员中心」+「首页」两个入口；原「学员版报告」「家长版报告」按钮已合并到 /me/，避免重复入口 #}
                <a href="/me/{{ luogu_uid }}" class="text-xs px-3 py-1.5 bg-emerald-100 text-emerald-700 rounded-md hover:bg-emerald-200">🎓 学员中心</a>
                <a href="/" class="text-xs px-3 py-1.5 bg-blue-100 text-blue-700 rounded-md hover:bg-blue-200">🏠 首页</a>
            </div>
        </div>
        {# v3.9 · 取消开发者/免责话术，改成"实用信息"，站在家长角度 #}
        <div class="mt-3 text-xs text-gray-600 bg-rose-50 border border-rose-200 rounded p-2">
            💡 建议打印后与教练约一次面谈，对照报告里的 5 个章节逐项讨论。
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
    {{ app_skin_head() }}
    <style>
        .card-shadow{box-shadow:0 10px 25px rgba(0,0,0,.06);}
    </style>
</head>
<body class="app-body p-4">
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
    {{ app_skin_head() }}
</head>
<body class="app-body p-6">
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
                            {# v3.9.30 · 加城市/省份列，之前缺失 #}
                            <th class="px-6 py-3">城市 / 省份</th>
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
                            <td class="px-6 py-3 text-gray-600">
                                {% if s.city or s.province %}
                                    {{ s.city or '—' }}{% if s.province %} <span class="text-xs text-gray-400">· {{ s.province }}</span>{% endif %}
                                {% else %}
                                    <span class="text-amber-600 text-xs">⚠ 未录</span>
                                {% endif %}
                            </td>
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
    {{ app_skin_head() }}
</head>
<body class="app-body p-6">
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
    {{ app_skin_head() }}
</head>
<body class="app-body p-6">
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
                    {# v3.9.30 · 城市/省份/性别/出生日期——之前缺失，家长说「录入了城市但看不到」 #}
                    <div class="flex">
                        <dt class="w-24 text-gray-500">城市 / 省份</dt>
                        <dd>
                            {% if student.city or student.province %}
                                <span class="font-semibold text-emerald-700">{{ student.city or '—' }}{% if student.province %} · {{ student.province }}{% endif %}</span>
                            {% else %}
                                <span class="text-amber-600">⚠ 未录入</span>
                            {% endif %}
                        </dd>
                    </div>
                    <div class="flex"><dt class="w-24 text-gray-500">年级</dt><dd>{{ student.grade or '—' }}</dd></div>
                    <div class="flex"><dt class="w-24 text-gray-500">性别 / 出生</dt><dd>
                        {% if student.gender %}{{ student.gender }}{% else %}—{% endif %}
                        {% if student.birth_date %}· {{ student.birth_date }}{% endif %}
                    </dd></div>
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
    {{ app_skin_head() }}
</head>
<body class="app-body p-6">
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

    v3.8 · 已登录学员：自动从 session 回填 form（无需重新输入 UID/姓名等）
    """
    form = _load_student_form_from_session()
    return render_template_string(
        GENERATE_FORM_HTML,
        form=form,
        server_key_hint=_get_server_key_hint(),
        gesp_default_year=date.today().year,
        validation_result=request.args.get("validation_result"),
    )


@app.route("/logout", methods=["GET", "POST"])
def student_logout():
    """v3.8 · 清除学员会话（"退出登录" 按钮）"""
    try:
        for _k in ("student_uid", "student_sid", "student_name", "student_login_at"):
            session.pop(_k, None)
    except Exception:
        pass
    next_url = request.args.get("next") or "/generate-form"
    # 防止开放重定向
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/generate-form"
    return redirect(next_url)


@app.route("/validate-cookies-v352", methods=["POST"])
def validate_cookies_v352():
    """v3.5.2 表单的 Cookie 预校验：仅校验 + 原地重渲染，不进入主提交流程"""
    form = request.form.to_dict()
    return render_template_string(
        GENERATE_FORM_HTML,
        form=form,
        server_key_hint=_get_server_key_hint(),
        gesp_default_year=date.today().year,
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
            gesp_default_year=date.today().year,
            error=f"请填写必填项：{', '.join(missing)}",
        ), 400

    # UID 格式
    luogu_uid = (form.get("uid") or "").strip()
    if not _re.match(r"^\d{6,10}$", luogu_uid):
        return render_template_string(
            GENERATE_FORM_HTML,
            form=form,
            server_key_hint=_get_server_key_hint(),
            gesp_default_year=date.today().year,
            error="UID 必须是 6-10 位数字",
        ), 400

    # v3.8 · 每日 1 次限流：最近 24 小时内该 UID 已生成过报告，则引导到 /me/<uid>
    try:
        from task_store import get_latest_done_task_for_uid
        existing = get_latest_done_task_for_uid(luogu_uid, since_hours=24)
        if existing:
            from flask import url_for as _uf
            me_url = _uf("student_me", luogu_uid=luogu_uid)
            # 计算剩余等待时间（精确到分钟）
            try:
                from datetime import datetime as _dt, timedelta as _td
                last = _dt.strptime(existing.get("created_at") or "", "%Y-%m-%d %H:%M:%S")
                next_at = last + _td(hours=24)
                remain = next_at - _dt.now()
                remain_min = max(1, int(remain.total_seconds() // 60))
                remain_txt = f"约 {remain_min // 60} 小时 {remain_min % 60} 分钟后可重新生成"
            except Exception:
                remain_txt = "明天再来生成新报告"
            return render_template_string(
                GENERATE_FORM_HTML,
                form=form,
                server_key_hint=_get_server_key_hint(),
                gesp_default_year=date.today().year,
                error=None,
                info=(
                    f"⚠️ UID {luogu_uid} 在最近 24 小时内已生成过报告（{remain_txt}）。"
                    f"请前往「个人中心」查看已生成的报告。"
                ),
                info_me_url=me_url,
            ), 429
    except Exception as _rate_e:
        app.logger.warning(f"每日限流检查失败: {_rate_e}")

    # PIPL 同意
    if not form.get("agree"):
        return render_template_string(
            GENERATE_FORM_HTML,
            form=form,
            server_key_hint=_get_server_key_hint(),
            gesp_default_year=date.today().year,
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
                # v3.9.18 · 主报告入口的学员档案更新后，失效 parent_subscribe.html/.md 缓存，
                # 避免之前 AI 生成的「未填城市」陈旧内容误导家长。
                try:
                    _latest_dir = _find_latest_report_dir(
                        luogu_uid, (form.get("real_name") or "").strip()
                    )
                    if _latest_dir:
                        for _fn in ("parent_subscribe.html", "parent_subscribe.md"):
                            _fp = _latest_dir / _fn
                            if _fp.exists():
                                _fp.unlink()
                except Exception:
                    pass
            finally:
                conn.close()
        else:
            new_sid = _admin_students.create_student(
                luogu_uid=luogu_uid,
                real_name=(form.get("real_name") or "").strip(),
                city=(form.get("city") or "").strip(),
                province=(form.get("province") or "").strip(),  # v3.8 · 省份
                grade=(form.get("grade") or "").strip(),
                gender=(form.get("gender") or "").strip() or None,
                school=(form.get("school") or "").strip() or None,
                birth_date=(form.get("birth_date") or "").strip() or None,
                registered_via="generate_form",
            )
            sid = int(new_sid)
            # 若有手机号，存到 guardians 表（v3.5.2 支持）
            phone = (form.get("phone") or "").strip()
            if phone and _re.match(r"^1[3-9]\d{9}$", phone):
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
            gesp_default_year=date.today().year,
            error=f"注册失败：{e}",
        ), 500

    # 1.5) v3.7 · 自录历史奖项（学员在表单内直填，提交时同步入库；新用户无需跳转 /me/<uid>）
    # v3.9.5 · 严格校验 + 失败显式日志（之前 try/except 静默吞错，导致 "填了 GESP 却不显示" 找不到原因）
    # v3.9.24 · 表单放宽：只填 level 也能保存（分数默认 60 = 及格线，年份默认今年），
    # 解决「用户只记得自己过了 GESP X 级，但忘了分数/年份」的常见场景。
    _award_log: list[str] = []
    try:
        _gl_raw = (form.get("gesp_level") or "").strip()
        _gs_raw = (form.get("gesp_score") or "").strip()
        _gy_raw = (form.get("gesp_year") or "").strip()
        if _gl_raw:  # v3.9.24 · 改为只校验 level 必填（score/year 走默认）
            # 校验
            try:
                _gl = int(_gl_raw)
                # 缺省值：分数 → 60（及格线），年份 → 今年
                _gs = int(_gs_raw) if _gs_raw.isdigit() else 60
                _gy = int(_gy_raw) if _gy_raw.isdigit() else date.today().year
            except (TypeError, ValueError) as _ve:
                app.logger.warning(
                    f"[self_register] GESP 字段类型错误 uid={luogu_uid} "
                    f"level={_gl_raw!r} score={_gs_raw!r} year={_gy_raw!r}: {_ve}"
                )
            else:
                if not (1 <= _gl <= 8):
                    app.logger.warning(f"[self_register] GESP 等级越界 uid={luogu_uid} level={_gl}")
                elif _gs_raw and not (0 <= _gs <= 100):
                    # v3.9.24 · 填了就校验，没填就跳过
                    app.logger.warning(f"[self_register] GESP 分数越界 uid={luogu_uid} score={_gs}")
                elif _gy_raw and not (2015 <= _gy <= date.today().year + 1):
                    app.logger.warning(f"[self_register] GESP 年份越界 uid={luogu_uid} year={_gy}")
                else:
                    try:
                        _admin_students.add_gesp_exam(
                            sid,
                            None,  # 触发 add_gesp_exam 按 year+level 自动查/建 competition
                            _gl,
                            _gs,
                            certificate_no=(form.get("gesp_certificate_no") or "").strip() or None,
                            award_year=_gy,
                            recorded_by="self_register",
                        )
                        _log_score = f"{_gs}分" if _gs_raw else "60分(默认)"
                        _log_year = f"{_gy}" if _gy_raw else f"{date.today().year}(默认)"
                        _award_log.append(f"GESP L{_gl}/{_log_year}={_log_score}")
                        app.logger.info(
                            f"[self_register] GESP 录入成功 uid={luogu_uid} sid={sid} "
                            f"L{_gl}/{_gy}={_gs}分 (score_raw={_gs_raw!r} year_raw={_gy_raw!r})"
                        )
                    except Exception as _ae:
                        # v3.9.5 · 不再静默：记 ERROR 级别，运维可直接看到
                        app.logger.error(
                            f"[self_register] GESP 写入数据库失败 uid={luogu_uid} sid={sid} "
                            f"L{_gl}/{_gy}={_gs}分: {_ae}",
                            exc_info=True,
                        )
    except Exception as _e:
        app.logger.error(f"[self_register] GESP 处理外层异常: {_e}", exc_info=True)

    try:
        _ct = (form.get("csp_competition_type") or "").strip()
        _cl = (form.get("csp_award_level") or "").strip()
        _cy = (form.get("csp_award_year") or "").strip()
        if _ct and _cl and _cy:
            _score_raw = (form.get("csp_score") or "").strip()
            _admin_students.add_csp_award(
                sid,
                _ct,
                _cl,
                int(_cy),
                actual_score=int(_score_raw) if _score_raw else None,
                province=(form.get("csp_province") or "").strip() or None,
                recorded_by="self_register",
            )
            _award_log.append(f"{_ct}/{_cl}/{_cy}")
    except Exception as _e:
        app.logger.warning(f"[self_register] CSP 录入失败: {_e}")

    if _award_log:
        app.logger.info(f"[self_register] uid={luogu_uid} 同步录入: {'; '.join(_award_log)}")

    # v3.8 · 写入学员会话（180 天）· 后续访问 /generate-form 自动回填
    try:
        _set_student_session(luogu_uid, int(sid), (form.get("real_name") or "").strip())
    except Exception as _e:
        app.logger.warning(f"[self_register] _set_student_session 失败: {_e}")

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
            insert_task(task_id, status="queued", message="排队中...", luogu_uid=luogu_uid)
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
    {{ app_skin_head() }}
    <style>
        .card-shadow{box-shadow:0 10px 25px rgba(0,0,0,.06);}
        .field-section{border-left:4px solid #059669;padding-left:12px;margin-bottom:16px;}
        .field-section h3{font-size:14px;font-weight:800;color:#047857;margin-bottom:8px;}
        .app-input,.app-select{width:100%;border:1px solid #d1d5db;border-radius:8px;padding:8px 10px;font-size:14px;transition:all .15s ease;}
        .app-input:focus,.app-select:focus{outline:none;border-color:#10b981;box-shadow:0 0 0 3px rgba(16,185,129,.15);}
        .app-label{font-size:12px;font-weight:600;color:#374151;}
        .app-btn{width:100%;font-weight:800;border-radius:10px;padding:12px 16px;transition:all .15s ease;cursor:pointer;font-size:15px;}
        .app-btn-primary{background:linear-gradient(135deg,#059669 0%,#0d9488 100%);color:#fff;border:none;}
        .app-btn-primary:hover{transform:translateY(-1px);box-shadow:0 8px 20px rgba(5,150,105,.3);}
        {# v3.9.11 · 401 invalid api key 时的输入框高亮 + 脉冲动画（提示用户改这里） #}
        .api-key-alert{border:2px solid #ef4444 !important;box-shadow:0 0 0 4px rgba(239,68,68,.18) !important;animation:key-pulse 1.6s ease-in-out infinite;}
        .api-key-alert:focus{border-color:#dc2626 !important;box-shadow:0 0 0 4px rgba(220,38,38,.28) !important;}
        @keyframes key-pulse{0%,100%{box-shadow:0 0 0 4px rgba(239,68,68,.18);}50%{box-shadow:0 0 0 8px rgba(239,68,68,.32);}}
    </style>
    {% if focus_api_key %}
    {# v3.9.11 · 自动滚动到 api_key 字段 + 弹出提示 "已为您定位" #}
    <script>
    window.addEventListener('load', function() {
        try {
            var el = document.getElementById('api_key');
            if (el) {
                el.scrollIntoView({behavior: 'smooth', block: 'center'});
                el.focus();
            }
        } catch (e) { console.error('focus_api_key err:', e); }
    });
    </script>
    {% endif %}
    </head>
<body class="app-body p-4">
<div class="max-w-2xl mx-auto py-6 space-y-4">

    <div class="text-center mb-4">
        <span class="inline-block px-3 py-1 bg-emerald-100 text-emerald-700 text-xs rounded-full">🎯 v3.5.2 统一入口</span>
        <h1 class="text-2xl font-extrabold text-gray-800 mt-2">🎓 AI 生成学习报告</h1>
        <p class="text-sm text-gray-500 mt-1">一次性填写 · 3 分钟出报告 · 3 版本报告 + 错题本 + 段位</p>
    </div>

    {% if form.get('_from_session') %}
    <div class="bg-emerald-50 border border-emerald-200 rounded-xl p-3 flex items-center justify-between">
        <div class="flex items-center gap-2 text-sm">
            <span class="text-2xl">👋</span>
            <div>
                <div class="font-bold text-emerald-800">已登录为 {{ form.get('_student_name') or '选手' }}（UID {{ form.get('uid','') }}）</div>
                <div class="text-[11px] text-emerald-600">🎉 选手信息已自动回填 · 如需换号请点"换号登录"</div>
            </div>
        </div>
        <a href="/logout?next=/generate-form" class="text-xs px-3 py-1.5 rounded-md bg-white border border-emerald-200 text-emerald-700 hover:bg-emerald-100 font-semibold">换号登录</a>
    </div>
    {% endif %}

    {% if error %}
    <div class="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-700">{{ error }}</div>
    {% endif %}

    {% if info %}
    <div class="bg-amber-50 border border-amber-300 rounded-lg p-4 text-sm text-amber-800">
        <p class="font-semibold mb-2">⏰ {{ info }}</p>
        {% if info_me_url %}
        <a href="{{ info_me_url }}" class="inline-block mt-1 px-4 py-2 rounded-md bg-amber-600 text-white text-xs font-bold hover:bg-amber-700">
            👉 查看已生成的报告
        </a>
        {% endif %}
    </div>
    {% endif %}

    {# v3.9.10 · 重试提示：报告生成失败时回到这里显示「已自动回填表单，请直接重提」 #}
    {% with _flashed = get_flashed_messages(with_categories=true) %}
    {% if _flashed %}
    <div class="space-y-2">
        {% for _cat, _msg in _flashed %}
        <div class="rounded-lg p-3 text-sm border {% if _cat == 'warning' %}bg-amber-50 border-amber-300 text-amber-800{% elif _cat == 'error' %}bg-rose-50 border-rose-300 text-rose-800{% elif _cat == 'success' %}bg-emerald-50 border-emerald-300 text-emerald-800{% else %}bg-indigo-50 border-indigo-300 text-indigo-800{% endif %}">
            <span class="font-semibold">[{{ _cat|upper }}]</span> {{ _msg }}
        </div>
        {% endfor %}
    </div>
    {% endif %}
    {% endwith %}

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
                    <!-- v3.8 · 改为「省+市」二级联动（折叠） -->
                    <details class="bg-emerald-50/40 border border-emerald-200 rounded-lg p-2.5 mt-1" open>
                        <summary class="cursor-pointer select-none text-xs font-bold text-emerald-800 flex items-center gap-1">
                            📍 选择所在省份/城市
                            <span class="text-[10px] text-gray-500 font-normal">（点击折叠）</span>
                        </summary>
                        <div class="grid grid-cols-1 sm:grid-cols-2 gap-2 mt-2">
                            <div>
                                <label class="app-label text-[11px]">省份 *</label>
                                <select name="province" id="province-select" required class="app-select mt-0.5 text-sm">
                                    <option value="">请选择省份</option>
                                    {% for r in region_options['regions'] %}
                                    <option value="{{ r.name }}" {% if form.get('province','') == r.name %}selected{% endif %}>{{ r.name }}</option>
                                    {% endfor %}
                                </select>
                            </div>
                            <div>
                                <label class="app-label text-[11px]">城市 *</label>
                                <select name="city" id="city-select" required class="app-select mt-0.5 text-sm">
                                    <option value="">请先选省份</option>
                                </select>
                            </div>
                        </div>
                        {% if form.get('city','') and not form.get('province','') %}
                        <input type="hidden" name="city_legacy" value="{{ form.get('city','') }}">
                        <p class="text-[10px] text-amber-600 mt-1">⚠️ 检测到旧版数据"{{ form.get('city','') }}"，请重新选择</p>
                        {% endif %}
                        <p class="text-[10px] text-gray-500 mt-1">💡 用于匹配本地科技特长生中学 / 强基大学 / 自招高中</p>
                    </details>
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

            <!-- v3.7 · 自录历史奖项（表单内直接录入，提交时同步写入数据库，避免新用户被引导到「未注册」页） -->
            <div class="mt-4 pt-4 border-t border-gray-200">
                <p class="text-xs font-bold text-gray-700 mb-2">🏆 填写最高奖项（可选 · 留空 = 跳过）</p>
                <p class="text-[10px] text-gray-500 mb-2">💡 只需填**最高**的那一项 GESP / CSP / NOIP / NOI；多个等级的同学，AI 会以最高档分析升学路径</p>
                <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
                    <!-- GESP 真考 · 表单内输入区 -->
                    <div class="bg-green-50 border border-green-200 rounded-lg p-3">
                        <div class="flex items-center gap-2 mb-2">
                            <span class="text-base">🎯</span>
                            <h4 class="text-sm font-bold text-green-800">GESP 真考（CCF 1-8 级）</h4>
                        </div>
                        <p class="text-[10px] text-gray-500 mb-1.5">💡 只需填**级别**（分数默认 60 = 及格线，年份默认今年）</p>
                        <div class="grid grid-cols-3 gap-2">
                            <select name="gesp_level" class="app-select text-xs">
                                <option value="">级别</option>
                                <option value="1">1 级</option>
                                <option value="2">2 级</option>
                                <option value="3">3 级</option>
                                <option value="4">4 级</option>
                                <option value="5">5 级</option>
                                <option value="6">6 级</option>
                                <option value="7">7 级</option>
                                <option value="8">8 级</option>
                            </select>
                            <input type="number" name="gesp_score" min="0" max="100" placeholder="分数" class="app-input text-xs">
                            <input type="number" name="gesp_year" min="2015" max="2030" placeholder="年份" class="app-input text-xs" value="{{ gesp_default_year }}">
                        </div>
                        <input type="text" name="gesp_certificate_no" placeholder="证书编号（可选）" class="app-input text-xs mt-2" value="{{ form.get('gesp_certificate_no','') }}">
                    </div>
                    <!-- CSP/NOIP/NOI · 表单内输入区 -->
                    <div class="bg-blue-50 border border-blue-200 rounded-lg p-3">
                        <div class="flex items-center gap-2 mb-2">
                            <span class="text-base">🏅</span>
                            <h4 class="text-sm font-bold text-blue-800">CSP / NOIP / NOI 奖项</h4>
                        </div>
                        <select name="csp_competition_type" class="app-select text-xs">
                            <option value="">比赛类型</option>
                            <option value="csp_j_pre">CSP-J 初赛</option>
                            <option value="csp_j_final">CSP-J 复赛</option>
                            <option value="csp_s_pre">CSP-S 初赛</option>
                            <option value="csp_s_final">CSP-S 复赛</option>
                            <option value="noip_1">NOIP 一等（省赛）</option>
                            <option value="noi_bronze">NOI 铜牌</option>
                            <option value="noi_silver">NOI 银牌</option>
                            <option value="noi_gold">NOI 金牌</option>
                        </select>
                        <select name="csp_award_level" class="app-select text-xs mt-2">
                            <option value="">奖项等级</option>
                            <option value="excellent">优秀</option>
                            <option value="first">一等</option>
                            <option value="second">二等</option>
                            <option value="third">三等</option>
                            <option value="bronze">铜牌</option>
                            <option value="silver">银牌</option>
                            <option value="gold">金牌</option>
                        </select>
                        <div class="grid grid-cols-2 gap-2 mt-2">
                            <input type="number" name="csp_award_year" min="2015" max="2030" placeholder="年份" class="app-input text-xs" value="{{ gesp_default_year }}">
                            <input type="number" name="csp_score" min="0" max="600" placeholder="分数（可选）" class="app-input text-xs" value="{{ form.get('csp_score','') }}">
                        </div>
                        <!-- v3.8 · CSP 省份改为下拉（折叠），便于本地政策匹配 -->
                        <details class="mt-2">
                            <summary class="text-[10px] text-gray-500 cursor-pointer select-none hover:text-emerald-700">📍 CSP 比赛省份（可选 · 点击选择）</summary>
                            <select name="csp_province" class="app-select text-xs mt-1">
                                <option value="">-- 请选择省赛省份 --</option>
                                {% for r in region_options['regions'] %}
                                <option value="{{ r.name }}" {% if form.get('csp_province','') == r.name %}selected{% endif %}>{{ r.name }}</option>
                                {% endfor %}
                            </select>
                        </details>
                    </div>
                </div>
                <p class="text-[10px] text-gray-400 mt-2 text-center">
                    💡 提交报告后，这些成绩会随学员档案一起入库；已注册学员也可在「个人中心→📥 自录历史奖项」继续追加
                </p>
            </div>
        </details>

        <!-- 4. OpenAI 配置（可选） -->
        <details class="field-section" {% if focus_api_key %}open{% endif %}>
            <summary class="cursor-pointer text-sm font-bold text-gray-600 hover:text-emerald-600">🤖 4. OpenAI 配置（可选 · 留空使用服务端默认）</summary>
            <div class="space-y-2 mt-3">
                <div>
                    <label class="app-label">
                        API Key
                        <button type="button" onclick="toggleKeyVisibility('api_key', this)"
                                class="ml-1 text-xs text-gray-500 hover:text-emerald-700"
                                title="点击查看 / 隐藏 API Key（避免填错）">👁 查看</button>
                    </label>
                    {# v3.9.15 · 加 oninput JS 校验：不是 sk- 开头时红边 + 提示，但不阻挡提交（启发式服务端兜底） #}
                    <input type="password" id="api_key" name="api_key"
                           value="{{ form.get('api_key','') }}"
                           {% if focus_api_key %}autofocus{% endif %}
                           oninput="validateApiKeyFormat(this)"
                           class="app-input mt-1 {% if focus_api_key %}api-key-alert{% endif %}"
                           placeholder="sk-...（七牛云/OpenAI 的 API Key，留空用服务端默认）">
                    <p id="api_key_hint" class="hidden text-xs mt-1"></p>
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

    // v3.8 · 省市二级联动
    (function(){
        var REGIONS = {{ region_options | tojson }};
        var $p = document.getElementById('province-select');
        var $c = document.getElementById('city-select');
        if (!$p || !$c) return;
        var initProvince = {{ (form.get('province','') or '') | tojson }};
        var initCity = {{ (form.get('city','') or '') | tojson }};
        function syncCities(provName, selCity) {
            $c.innerHTML = '';
            var opt0 = document.createElement('option');
            opt0.value = '';
            opt0.textContent = '请选择城市';
            $c.appendChild(opt0);
            var region = REGIONS.regions.find(function(r){ return r.name === provName; });
            if (!region) return;
            (region.cities || []).forEach(function(city){
                var opt = document.createElement('option');
                opt.value = city;
                opt.textContent = city;
                if (city === selCity) opt.selected = true;
                $c.appendChild(opt);
            });
        }
        $p.addEventListener('change', function(){ syncCities($p.value, ''); });
        if (initProvince) {
            $p.value = initProvince;
            syncCities(initProvince, initCity);
        }
    })();
</script>
</body>
</html>
"""


@app.route("/select-mode", methods=["GET", "POST"])
def select_mode():
    """v3.6 老用户快速入口：输 UID → 直接展示该选手所有历史报告（html/pdf）"""
    import re as _re
    import os as _os
    _uid_guide_exists = _os.path.exists(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "static", "uid_guide.png"))
    if request.method == "GET":
        return render_template_string(SELECT_MODE_HTML, error=None, form={}, static_exists_uid_guide=_uid_guide_exists)
    # POST 接收 luogu_uid → 校验 → 扫 reports/ 下该 UID 的所有报告
    luogu_uid = (request.form.get("luogu_uid") or "").strip()
    if not _re.match(r"^\d{6,10}$", luogu_uid):
        return render_template_string(
            SELECT_MODE_HTML,
            error="请输入 6-10 位洛谷 UID",
            form={"luogu_uid": luogu_uid},
            static_exists_uid_guide=_uid_guide_exists,
        ), 400
    # 1) 已注册 → 走 /me/<uid>（个人中心也带历史报告，但入口直达列表更快）
    stu = _admin_students.get_student_by_uid(luogu_uid)
    if stu:
        return redirect(url_for("student_me", luogu_uid=luogu_uid))
    # 2) 未注册 / 任意用户 → 直接扫 reports/ 找该 UID 所有报告
    reports = _list_reports_for_uid(luogu_uid)
    if not reports:
        return render_template_string(
            SELECT_MODE_HTML,
            error=f"UID {luogu_uid} 暂无历史报告（请先在首页生成新报告）",
            form={"luogu_uid": luogu_uid},
            static_exists_uid_guide=_uid_guide_exists,
        ), 404
    return render_template_string(LIST_REPORTS_HTML, luogu_uid=luogu_uid, reports=reports)


SELECT_MODE_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>选择报告版本 · 信竞 AI 报告 v3.5.2</title>
    <script src="https://cdn.tailwindcss.com"></script>
    {{ app_skin_head() }}
    <style>
        .card-shadow{box-shadow:0 10px 25px rgba(0,0,0,.06);}
        .big-btn{display:flex;align-items:center;justify-content:center;width:100%;border-radius:12px;padding:14px;font-weight:800;transition:all .15s ease;cursor:pointer;}
        .big-btn-primary{background:linear-gradient(135deg,#059669 0%,#0d9488 100%);color:#fff;}
        .big-btn-primary:hover{transform:translateY(-1px);box-shadow:0 8px 20px rgba(5,150,105,.3);}
        .big-btn-secondary{background:#fff;color:#047857;border:2px solid #6ee7b7;}
        .big-btn-secondary:hover{background:#ecfdf5;}
    </style>
    {{ app_skin_head() }}
</head>
<body class="app-body p-4">
<div class="max-w-2xl mx-auto py-6 space-y-4">

    <div class="bg-white rounded-2xl card-shadow p-6">
        <div class="text-center mb-4">
            <span class="inline-block px-3 py-1 bg-emerald-100 text-emerald-700 text-xs rounded-full">📁 历史报告</span>
            <h1 class="text-2xl font-extrabold text-gray-800 mt-2">📚 查看历史报告</h1>
            <p class="text-sm text-gray-500 mt-1">输入洛谷 UID · 一次性展示该选手历次生成的全部报告（HTML / PDF）</p>
        </div>

        <form method="post" class="space-y-3">
            <div>
                <label class="block text-sm font-bold text-gray-700 mb-1">洛谷 UID <span class="text-red-500">*</span></label>
                <input name="luogu_uid" inputmode="numeric" pattern="\\d{6,10}" required placeholder="请输入 6-10 位数字 UID" class="w-full border border-gray-300 rounded-lg px-3 py-2.5 focus:border-emerald-500 focus:ring-2 focus:ring-emerald-200" value="{{ form.get('luogu_uid','') }}">
                <p class="text-xs text-gray-400 mt-1">没找到该 UID 的报告？请先到 <a href="/" class="text-emerald-600 hover:underline">首页</a> 生成新报告</p>

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

            <button type="submit" class="big-btn big-btn-primary">🔍 查看历史报告</button>
        </form>
    </div>

    <p class="text-center text-xs text-gray-400">信竞 AI 报告 · v3.5.2</p>
</div>
</body>
</html>
"""

LIST_REPORTS_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>历史报告列表 · UID {{ luogu_uid }} · v3.6</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body{background:linear-gradient(135deg,#ecfdf5 0%,#f0f9ff 100%);min-height:100vh;font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;}
        .card-shadow{box-shadow:0 10px 25px rgba(0,0,0,.06);}
        .file-pill{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:9999px;font-size:12px;font-weight:600;text-decoration:none;transition:all .15s ease;border:1px solid transparent;}
        .pill-html{background:#dbeafe;color:#1d4ed8;}
        .pill-html:hover{background:#bfdbfe;border-color:#3b82f6;}
        .pill-pdf{background:#fee2e2;color:#b91c1c;}
        .pill-pdf:hover{background:#fecaca;border-color:#ef4444;}
        .pill-md{background:#f3f4f6;color:#4b5563;}
        .pill-md:hover{background:#e5e7eb;border-color:#6b7280;}
        .pill-missing{background:#f3f4f6;color:#9ca3af;cursor:not-allowed;opacity:.55;}
        .report-card{background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:16px 18px;transition:all .2s ease;}
        .report-card:hover{border-color:#10b981;box-shadow:0 8px 20px rgba(16,185,129,.10);}
        .badge{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;color:#6b7280;background:#f9fafb;padding:2px 6px;border-radius:4px;}
    </style>
</head>
<body class="p-4">
<div class="max-w-3xl mx-auto py-6 space-y-4">

    <!-- 顶部 -->
    <div class="bg-white rounded-2xl card-shadow p-5">
        <div class="flex items-start justify-between gap-3 flex-wrap">
            <div>
                <span class="inline-block px-3 py-1 bg-emerald-100 text-emerald-700 text-xs rounded-full">📁 历史报告</span>
                <h1 class="text-2xl font-extrabold text-gray-800 mt-2">📚 UID {{ luogu_uid }} 的历史报告</h1>
                <p class="text-sm text-gray-500 mt-1">共找到 <b class="text-emerald-700">{{ reports|length }}</b> 份历史报告（按时间倒序）</p>
            </div>
            <a href="/select-mode" class="text-sm text-emerald-700 hover:underline whitespace-nowrap">← 返回重新输入</a>
        </div>
    </div>

    <!-- 报告列表 -->
    {% for r in reports %}
    <div class="report-card">
        <div class="flex items-start justify-between gap-3 flex-wrap">
            <div class="min-w-0 flex-1">
                <div class="flex items-center gap-2 flex-wrap">
                    <span class="text-base font-bold text-gray-800 truncate">📄 {{ r.task_id }} <span class="text-gray-400">·</span> {{ r.name }}</span>
                    {% if r.is_latest %}<span class="text-[10px] px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-700 font-bold">最新</span>{% endif %}
                </div>
                <div class="text-xs text-gray-500 mt-1.5 flex items-center gap-2 flex-wrap">
                    <span>🕒 {{ r.mtime_str }}</span>
                    <span class="opacity-40">|</span>
                    <span class="badge">{{ r.file_count }} 个文件</span>
                </div>
                <!-- 文件链接 -->
                <div class="mt-3 flex items-center gap-1.5 flex-wrap">
                    {% for f in r.files %}
                    {% if f.exists %}
                    <a href="{{ f.url }}" target="_blank" class="file-pill pill-{{ f.kind }}">{% if f.kind=='html' %}🌐{% elif f.kind=='pdf' %}📕{% else %}📝{% endif %} {{ f.label }}</a>
                    {% else %}
                    <span class="file-pill pill-missing" title="该文件暂未生成">{% if f.kind=='html' %}🌐{% elif f.kind=='pdf' %}📕{% else %}📝{% endif %} {{ f.label }} · 未生成</span>
                    {% endif %}
                    {% endfor %}
                </div>
            </div>
        </div>
    </div>
    {% endfor %}

    {% if reports|length == 0 %}
    <div class="bg-white rounded-2xl card-shadow p-6 text-center text-gray-500 text-sm">
        未找到该 UID 的历史报告。
    </div>
    {% endif %}

    <!-- 底部 -->
    <div class="text-center text-xs text-gray-400 space-y-1">
        <div>没有你要的报告？<a href="/" class="text-emerald-600 hover:underline">去首页生成新报告 →</a></div>
        <div>信竞 AI 报告 · v3.6</div>
    </div>
</div>
<script>
// v3.9.15 · API Key 可见性切换（避免填错：填完能立刻看到）
function toggleKeyVisibility(inputId, btn) {
    var el = document.getElementById(inputId);
    if (!el) return;
    if (el.type === 'password') {
        el.type = 'text';
        if (btn) btn.innerHTML = '🙈 隐藏';
    } else {
        el.type = 'password';
        if (btn) btn.innerHTML = '👁 查看';
    }
}

// v3.9.15 · 实时校验 API Key 格式：不以 sk- 开头时红边 + 提示
// 注意：这是提示性校验，不阻挡提交（服务端还有启发式兜底）
function validateApiKeyFormat(input) {
    var hint = document.getElementById('api_key_hint');
    if (!hint) return;
    var v = (input.value || '').trim();
    if (!v) {
        // 空 → 用服务端默认，不提示
        hint.classList.add('hidden');
        input.style.borderColor = '';
        return;
    }
    var looksLikeKey = v.startsWith('sk-') || v.startsWith('key-') || v.startsWith('API-') || v.length >= 32;
    if (looksLikeKey) {
        hint.classList.add('hidden');
        input.style.borderColor = '';
    } else {
        hint.classList.remove('hidden');
        hint.className = 'text-xs mt-1 text-rose-600';
        hint.innerHTML = '⚠️ 这串不像 API Key（应以 <code>sk-</code> 开头）。<br>· 如果你只是想用服务端默认 Key，请<strong>留空</strong>本字段<br>· 七牛云控制台路径：AI 服务 → API Key → 复制 <code>sk-...</code> 开头的串';
        input.style.borderColor = '#f43f5e';
    }
}

// v3.9.16 · 页面加载时立即校验一次（重试场景下表单已回填值）
document.addEventListener('DOMContentLoaded', function() {
    var el = document.getElementById('api_key');
    if (el && el.value) validateApiKeyFormat(el);
    // v3.9.16 · GESP 三字段都填了才标"已填好"，给用户视觉反馈
    function updateGespState() {
        var l = document.querySelector('[name="gesp_level"]');
        var s = document.querySelector('[name="gesp_score"]');
        var y = document.querySelector('[name="gesp_year"]');
        if (!l || !s || !y) return;
        var filled = l.value && s.value && y.value;
        var gespDiv = l.closest('.bg-green-50');
        if (gespDiv) {
            if (filled) {
                gespDiv.style.borderColor = '#10B981';  // 绿
                gespDiv.style.borderWidth = '2px';
            } else {
                gespDiv.style.borderColor = '';
                gespDiv.style.borderWidth = '';
            }
        }
    }
    ['gesp_level', 'gesp_score', 'gesp_year'].forEach(function(n) {
        var e = document.querySelector('[name="' + n + '"]');
        if (e) e.addEventListener('input', updateGespState);
    });
    updateGespState();
});
</script>
</body>
</html>
"""

# GENERATE_FORM_HTML ends at line ~6329

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
                                # v3.9.26 · duration_days=0 表示"不限期"（不立即过期），
                                # 设 expires_at=NULL。否则 query 中 `expires_at > now` 立即失败，
                                # parent_invite 用户刚激活就被判定"已过期"，has_parent_sub 永远 False。
                                _dur = int(row_dict.get("duration_days") or 0)
                                if _dur <= 0:
                                    _expires_sql = "NULL"
                                else:
                                    _expires_sql = f"datetime('now', '+{_dur} days')"
                                conn.execute(
                                    f"UPDATE activation_codes "
                                    f"SET redeemed_at = datetime('now'), "
                                    f"    student_id = ?, "
                                    f"    expires_at = {_expires_sql} "
                                    f"WHERE code = ?",
                                    (stu["id"], code),
                                )
                                # v3.9.26 · parent_invite 是「家长订阅邀请码」(0 元 · 客服手动派发)，
                                # 激活时等价于同时激活一份 30 天 parent_sub 订阅。
                                # 原因：v3.9 之前生成的 parent_invite 邀请码只 bind 到 student_id，
                                # 不会让 has_parent_sub(SKU='parent_sub') 通过 → AI 讲题页仍显示「需家长订阅」🔒。
                                # 此举对已激活的 6 个 parent_invite 码（id 16/17/18/19/20/21）
                                # 同样安全：INSERT OR IGNORE 用新 code 不与旧码冲突，user 重新激活时才走这里。
                                sku_value = row_dict.get("sku", "parent_sub")
                                if sku_value == "parent_invite":
                                    _auto_code = f"AUTO-{code}-{stu['id']}"
                                    conn.execute(
                                        "INSERT OR IGNORE INTO activation_codes "
                                        "(code, sku, duration_days, student_id, redeemed_at, expires_at, created_by) "
                                        "VALUES (?, 'parent_sub', 30, ?, "
                                        "        datetime('now'), datetime('now', '+30 days'), 'auto-from-parent_invite')",
                                        (_auto_code, stu["id"]),
                                    )
                                    app.logger.info(
                                        f"[redeem] parent_invite {code} 已绑定到 sid={stu['id']} uid={student_uid}，"
                                        f"并自动创建 30 天 parent_sub 订阅（_auto_code={_auto_code}）"
                                    )
                                conn.commit()
                            finally:
                                conn.close()
                            success = {
                                "code": code,
                                "sku": sku_value,
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
    # v3.9 · 新增：家长订阅邀请码（admin 后台生成后，客服发给家长，扫码获得）
    # duration=0 表示"不激活订阅，只是个通行码"；允许多人使用同一码
    ("parent_invite",    "家长订阅邀请码", 0,  "0 元 · 客服手动派发，多人通用 · 触发家长订阅版生成"),
]
_SKU_DURATIONS = {v[0]: v[2] for v in _SKU_PRESETS}
_SKU_LABELS = {v[0]: v[1] for v in _SKU_PRESETS}


def _generate_activation_code(sku: str) -> str:
    """生成不重复的兑换码：<SKU 前缀>-<8 位 A-Z0-9>
    前缀取 sku 单词首字母（parent_sub → PS · popularize_camp → PJC · improve_camp → IJC），
    与历史生成码兼容（PJC-*/IJC-* 已存在 14 个）。
    """
    prefix_map = {
        "parent_sub": "PS",
        "popularize_camp": "PJC",
        "improve_camp": "IJC",
        "parent_invite": "PINV",  # v3.9 · 家长订阅邀请码
    }
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
                    # v3.9 · parent_invite 是"邀请码"类型（不激活任何订阅，duration=0 是合法值），
                    #         其他 SKU 必须 1-3650 天
                    skip_duration_check = (sku == "parent_invite")
                    if sku == "parent_invite":
                        duration_days = 0
                    elif duration_days < 1 or duration_days > 3650:
                        flash, flash_type = "有效期需在 1-3650 天之间", "error"
                    if not flash or skip_duration_check:
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
    {{ app_skin_head() }}
    <style>
        .card{background:#fff;border-radius:14px;box-shadow:0 1px 3px rgba(0,0,0,.06);padding:18px;}
        .badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;}
        .code-cell{font-family:"JetBrains Mono","SF Mono",Consolas,monospace;letter-spacing:.5px;}
        .highlight-row{background:#fef9c3 !important;}
        .highlight-cell{background:#fde68a;padding:1px 4px;border-radius:3px;}
    </style>
</head>
<body class="app-body min-h-screen p-4">
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
    {{ app_skin_head() }}
    <style>
        .sku-card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:14px;text-align:center;}
        .sku-card.pro{border-color:#f59e0b;background:linear-gradient(135deg,#fffbeb,#fef3c7);}
        .sku-card.parent{border-color:#3b82f6;background:linear-gradient(135deg,#eff6ff,#dbeafe);}
        .sku-card.camp-j{border-color:#a855f7;background:linear-gradient(135deg,#faf5ff,#f3e8ff);}
        .sku-card.camp-s{border-color:#ef4444;background:linear-gradient(135deg,#fef2f2,#fee2e2);}
    </style>
</head>
<body class="app-body flex items-center justify-center p-4">
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
    {{ app_skin_head() }}
    <style>
    </style>
</head>
<body class="app-body flex items-center justify-center p-4">
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
# v3.9 · 升学政策管理（admin 后台维护 → 家长报告自动引用）
# ============================================================
# 路由：
#  - /admin/policies                          列出所有政策学校（按类型+城市）
#  - /admin/policies/new                      新建（GET/POST）
#  - /admin/policies/<id>/edit                编辑（GET/POST）
#  - /admin/policies/<id>/delete              删除（POST）
# 数据源：task_store.policy_match_schools 表（v3.5.2 种子 32 所）
# 数据由 admin 手动维护；用户要求"由 admin 去获取" → 满足

_POLICY_SCHOOL_TYPES = [
    ("tech_talent_junior",  "科技特长生（初中）"),
    ("self_enroll_senior",  "自招/特长生（高中）"),
    ("qiangji_university",  "强基计划（大学）"),
]
_POLICY_TYPE_LABEL = dict(_POLICY_SCHOOL_TYPES)
_POLICY_TARGET_STAGES = [("primary", "小学"), ("junior", "初中"), ("senior", "高中")]


# v3.9 · 校徽管理（强基 39 校 PNG 上传/删除/列表）
# 强基 39 校 + 校色 + 拼音缩写（与首页 ticker 同步）
QIANGJI_SCHOOL_LIST = [
    ('pku',   '北京大学',         '#A40027', 'PKU',  '北京'),
    ('thu',   '清华大学',         '#660874', 'THU',  '北京'),
    ('ruc',   '中国人民大学',     '#C8161D', 'RUC',  '北京'),
    ('buaa',  '北京航空航天大学', '#0050B3', 'BUAA', '北京'),
    ('bit',   '北京理工大学',     '#1A6E3A', 'BIT',  '北京'),
    ('cau',   '中国农业大学',     '#D4A017', 'CAU',  '北京'),
    ('bnu',   '北京师范大学',     '#003D7C', 'BNU',  '北京'),
    ('muc',   '中央民族大学',     '#3F4A5C', 'MUC',  '北京'),
    ('nankai','南开大学',         '#591F5C', 'NKU',  '天津'),
    ('tju',   '天津大学',         '#005BAA', 'TJU',  '天津'),
    ('dlut',  '大连理工大学',     '#006747', 'DUT',  '辽宁'),
    ('neu',   '东北大学',         '#C9A227', 'NEU',  '辽宁'),
    ('jlu',   '吉林大学',         '#9B1D20', 'JLU',  '吉林'),
    ('hit',   '哈尔滨工业大学',   '#1F3A93', 'HIT',  '黑龙江'),
    ('fdu',   '复旦大学',         '#B71C2A', 'FDU',  '上海'),
    ('tongji','同济大学',         '#003E7E', 'TJ',   '上海'),
    ('sjtu',  '上海交通大学',     '#0A246A', 'SJTU', '上海'),
    ('ecnu',  '华东师范大学',     '#0B6E4F', 'ECNU', '上海'),
    ('nju',   '南京大学',         '#6B2A78', 'NJU',  '江苏'),
    ('seu',   '东南大学',         '#D4A017', 'SEU',  '江苏'),
    ('zju',   '浙江大学',         '#B71C2A', 'ZJU',  '浙江'),
    ('ustc',  '中国科学技术大学', '#C0392B', 'USTC', '安徽'),
    ('xmu',   '厦门大学',         '#B8923A', 'XMU',  '福建'),
    ('sdu',   '山东大学',         '#003E7E', 'SDU',  '山东'),
    ('ouc',   '中国海洋大学',     '#005BAA', 'OUC',  '山东'),
    ('whu',   '武汉大学',         '#591F5C', 'WHU',  '湖北'),
    ('hust',  '华中科技大学',     '#0B6E4F', 'HUST', '湖北'),
    ('csu',   '中南大学',         '#D4A017', 'CSU',  '湖南'),
    ('hnu',   '湖南大学',         '#9B1D20', 'HNU',  '湖南'),
    ('nudt',  '国防科技大学',     '#0B5345', 'NUDT', '湖南'),
    ('sysu',  '中山大学',         '#005BAA', 'SYSU', '广东'),
    ('scut',  '华南理工大学',     '#B71C2A', 'SCUT', '广东'),
    ('scu',   '四川大学',         '#C9A227', 'SCU',  '四川'),
    ('cqu',   '重庆大学',         '#1F3A93', 'CQU',  '重庆'),
    ('uestc', '电子科技大学',     '#1F3A93', 'UESTC','四川'),
    ('xjtu',  '西安交通大学',     '#B71C2A', 'XJTU', '陕西'),
    ('nwpu',  '西北工业大学',     '#005BAA', 'NPU',  '陕西'),
    ('nwafu', '西北农林科技大学', '#0B6E4F', 'NWAFU','陕西'),
    ('lzu',   '兰州大学',         '#005BAA', 'LZU',  '甘肃'),
]
QIANGJI_KEY_TO_INFO = {s[0]: s for s in QIANGJI_SCHOOL_LIST}


@app.route("/admin/schools", methods=["GET"])
def admin_schools_list():
    """列出 39 强基校徽 + 当前已上传状态（缺失时显示默认 SVG 占位）"""
    auth_redirect = require_admin_auth()
    if auth_redirect is not None:
        return auth_redirect
    schools_dir = _ROOT / "static" / "schools"
    schools_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for s in QIANGJI_SCHOOL_LIST:
        key, name, color, abbr, province = s
        png_path = schools_dir / f"{key}.png"
        rows.append({
            "key": key,
            "name": name,
            "color": color,
            "abbr": abbr,
            "province": province,
            "has_png": png_path.exists(),
            "png_size": png_path.stat().st_size if png_path.exists() else 0,
            "png_mtime": png_path.stat().st_mtime if png_path.exists() else 0,
        })
    notice = request.args.get("notice", "")
    notice_type = request.args.get("notice_type", "success")
    html = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>校徽管理 · Luogu-AI-Report</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>.school-img{width:32px;height:32px;object-fit:contain;background:#fff;border-radius:6px;padding:3px;box-shadow:0 1px 3px rgba(0,0,0,.1);}
.school-badge{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;border-radius:50%;color:#fff;font-weight:800;font-size:10px;letter-spacing:-.02em;line-height:1;box-shadow:inset 0 0 0 1px rgba(255,255,255,.2),0 1px 3px rgba(0,0,0,.2);}</style>
</head><body class="bg-gray-50 min-h-screen">
<div class="max-w-7xl mx-auto p-6">
  <div class="flex items-center justify-between mb-6">
    <div>
      <h1 class="text-2xl font-bold text-gray-900">🎓 校徽管理</h1>
      <p class="text-sm text-gray-500 mt-1">强基计划 39 所高校校徽 · 支持上传/删除 PNG 校徽</p>
    </div>
    <div class="flex gap-2 text-sm">
      <a href="/admin" class="px-3 py-1.5 rounded bg-gray-100 text-gray-700 hover:bg-gray-200">← 返回后台</a>
      <a href="/" class="px-3 py-1.5 rounded bg-gray-100 text-gray-700 hover:bg-gray-200">返回首页</a>
    </div>
  </div>
  {% if notice %}
  <div class="mb-4 px-4 py-3 rounded-lg {{ 'bg-emerald-50 text-emerald-700' if notice_type == 'success' else 'bg-rose-50 text-rose-700' }}">{{ notice }}</div>
  {% endif %}
  <div class="bg-white rounded-lg shadow border border-gray-200 overflow-hidden">
    <table class="min-w-full text-sm">
      <thead class="bg-gray-50">
        <tr class="text-left text-gray-600">
          <th class="px-4 py-3">预览</th>
          <th class="px-4 py-3">校徽缩写</th>
          <th class="px-4 py-3">校名</th>
          <th class="px-4 py-3">省份</th>
          <th class="px-4 py-3">状态</th>
          <th class="px-4 py-3">操作</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-gray-100">
        {% for r in rows %}
        <tr class="hover:bg-gray-50">
          <td class="px-4 py-3">
            {% if r.has_png %}
              <img class="school-img" src="/static/schools/{{ r.key }}.png?v={{ r.png_mtime }}" alt="{{ r.name }}">
            {% else %}
              <span class="school-badge" style="background:{{ r.color }}">{{ r.abbr }}</span>
            {% endif %}
          </td>
          <td class="px-4 py-3 font-mono text-xs text-gray-500">{{ r.abbr }}</td>
          <td class="px-4 py-3 font-semibold text-gray-800">{{ r.name }}</td>
          <td class="px-4 py-3 text-gray-500">{{ r.province }}</td>
          <td class="px-4 py-3">
            {% if r.has_png %}
              <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-emerald-50 text-emerald-700">✅ {{ r.png_size }} 字节</span>
            {% else %}
              <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-amber-50 text-amber-700">⚠️ 未上传</span>
            {% endif %}
          </td>
          <td class="px-4 py-3">
            <form method="POST" action="/admin/schools/upload" enctype="multipart/form-data" class="inline">
              <input type="hidden" name="school_key" value="{{ r.key }}">
              <label class="cursor-pointer px-2 py-1 rounded text-xs bg-blue-50 text-blue-700 hover:bg-blue-100">
                {{ '替换' if r.has_png else '上传' }}
                <input type="file" name="logo_file" accept="image/png,image/jpeg,image/svg+xml" class="hidden" onchange="this.form.submit()">
              </label>
            </form>
            {% if r.has_png %}
            <form method="POST" action="/admin/schools/delete" class="inline" onsubmit="return confirm('删除 {{ r.name }} 校徽？将恢复默认 SVG 占位。');">
              <input type="hidden" name="school_key" value="{{ r.key }}">
              <button type="submit" class="ml-1 px-2 py-1 rounded text-xs bg-rose-50 text-rose-700 hover:bg-rose-100">删除</button>
            </form>
            {% endif %}
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  <p class="text-xs text-gray-500 mt-4">
    💡 推荐尺寸 64×64 PNG（透明背景），最大 1MB。上传后立即在首页"强基 39 校"滚动展示生效。
  </p>
</div></body></html>"""
    return render_template_string(html, rows=rows, notice=notice, notice_type=notice_type)


@app.route("/admin/schools/upload", methods=["POST"])
def admin_schools_upload():
    """上传校徽 PNG（覆盖式）"""
    auth_redirect = require_admin_auth()
    if auth_redirect is not None:
        return auth_redirect
    school_key = (request.form.get("school_key") or "").strip().lower()
    if school_key not in QIANGJI_KEY_TO_INFO:
        return redirect(url_for("admin_schools_list", notice=f"未知的校徽 key：{school_key}", notice_type="error"))
    f = request.files.get("logo_file")
    if not f or not f.filename:
        return redirect(url_for("admin_schools_list", notice="未选择文件", notice_type="error"))
    # 校验后缀
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in {".png", ".jpg", ".jpeg", ".svg"}:
        return redirect(url_for("admin_schools_list", notice=f"不支持的文件格式：{ext}（仅 PNG/JPG/SVG）", notice_type="error"))
    # 限制大小（≤1MB）
    data = f.read()
    if len(data) > 1024 * 1024:
        return redirect(url_for("admin_schools_list", notice=f"文件过大：{len(data)} 字节（最大 1MB）", notice_type="error"))
    schools_dir = _ROOT / "static" / "schools"
    schools_dir.mkdir(parents=True, exist_ok=True)
    out_path = schools_dir / f"{school_key}.png"  # 统一存为 .png
    out_path.write_bytes(data)
    return redirect(url_for("admin_schools_list", notice=f"已上传 {QIANGJI_KEY_TO_INFO[school_key][1]} 校徽（{len(data)} 字节）", notice_type="success"))


@app.route("/admin/schools/delete", methods=["POST"])
def admin_schools_delete():
    """删除校徽（恢复默认 SVG 占位）"""
    auth_redirect = require_admin_auth()
    if auth_redirect is not None:
        return auth_redirect
    school_key = (request.form.get("school_key") or "").strip().lower()
    if school_key not in QIANGJI_KEY_TO_INFO:
        return redirect(url_for("admin_schools_list", notice=f"未知的校徽 key：{school_key}", notice_type="error"))
    schools_dir = _ROOT / "static" / "schools"
    out_path = schools_dir / f"{school_key}.png"
    if out_path.exists():
        out_path.unlink()
    return redirect(url_for("admin_schools_list", notice=f"已删除 {QIANGJI_KEY_TO_INFO[school_key][1]} 校徽（已恢复默认 SVG 占位）", notice_type="success"))


@app.route("/admin/policies")
def admin_policies_list():
    """列出所有政策匹配学校（按类型+城市分组）"""
    auth_redirect = require_admin_auth()
    if auth_redirect is not None:
        return auth_redirect
    from task_store import _get_conn
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, school_name, school_type, target_stage, city, province, "
            "       policy_summary, requires_competition, enrollment_count, "
            "       policy_url, priority, effective_year, last_updated_at "
            "FROM policy_match_schools "
            "ORDER BY school_type, province, city, priority, school_name"
        ).fetchall()
        total = len(rows)
        groups = {}
        for r in rows:
            d = dict(r)
            t = d["school_type"]
            if t not in groups:
                groups[t] = {"label": _POLICY_TYPE_LABEL.get(t, t), "rows": []}
            groups[t]["rows"].append(d)

        last_updated = conn.execute(
            "SELECT MAX(last_updated_at) FROM policy_match_schools"
        ).fetchone()[0] or "—"

        return render_template_string(
            ADMIN_POLICIES_HTML,
            groups=groups,
            total=total,
            last_updated=last_updated,
            school_types=_POLICY_SCHOOL_TYPES,
            target_stages=_POLICY_TARGET_STAGES,
            notice=request.args.get("notice", ""),
            notice_type=request.args.get("notice_type", "success"),
        )
    finally:
        conn.close()


@app.route("/admin/policies/new", methods=["GET", "POST"])
def admin_policies_new():
    auth_redirect = require_admin_auth()
    if auth_redirect is not None:
        return auth_redirect
    if request.method == "POST":
        from task_store import _get_conn
        conn = _get_conn()
        try:
            conn.execute(
                """INSERT INTO policy_match_schools
                   (school_name, school_type, target_stage, city, province,
                    policy_summary, requires_competition, enrollment_count,
                    policy_url, priority, effective_year)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    (request.form.get("school_name") or "").strip(),
                    (request.form.get("school_type") or "").strip(),
                    (request.form.get("target_stage") or "").strip(),
                    (request.form.get("city") or "").strip() or "全国",
                    (request.form.get("province") or "").strip() or "全国",
                    (request.form.get("policy_summary") or "").strip() or None,
                    (request.form.get("requires_competition") or "").strip() or None,
                    int(request.form.get("enrollment_count") or 0) or None,
                    (request.form.get("policy_url") or "").strip() or None,
                    int(request.form.get("priority") or 100),
                    int(request.form.get("effective_year") or 2026),
                ),
            )
            conn.commit()
            return redirect(url_for("admin_policies_list", notice="政策学校已添加", notice_type="success"))
        except Exception as e:
            conn.rollback()
            return redirect(url_for("admin_policies_new", notice=f"添加失败: {e}", notice_type="error"))
        finally:
            conn.close()
    return render_template_string(
        ADMIN_POLICY_FORM_HTML,
        policy={},
        action_url="/admin/policies/new",
        school_types=_POLICY_SCHOOL_TYPES,
        target_stages=_POLICY_TARGET_STAGES,
        notice=request.args.get("notice", ""),
        notice_type=request.args.get("notice_type", "error"),
    )


@app.route("/admin/policies/<int:policy_id>/edit", methods=["GET", "POST"])
def admin_policies_edit(policy_id: int):
    auth_redirect = require_admin_auth()
    if auth_redirect is not None:
        return auth_redirect
    from task_store import _get_conn
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM policy_match_schools WHERE id = ?", (policy_id,)
        ).fetchone()
        if not row:
            return redirect(url_for("admin_policies_list", notice="政策学校不存在", notice_type="error"))
        policy = dict(row)

        if request.method == "POST":
            conn.execute(
                """UPDATE policy_match_schools SET
                   school_name = ?, school_type = ?, target_stage = ?,
                   city = ?, province = ?, policy_summary = ?,
                   requires_competition = ?, enrollment_count = ?,
                   policy_url = ?, priority = ?, effective_year = ?,
                   last_updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (
                    (request.form.get("school_name") or "").strip(),
                    (request.form.get("school_type") or "").strip(),
                    (request.form.get("target_stage") or "").strip(),
                    (request.form.get("city") or "").strip() or "全国",
                    (request.form.get("province") or "").strip() or "全国",
                    (request.form.get("policy_summary") or "").strip() or None,
                    (request.form.get("requires_competition") or "").strip() or None,
                    int(request.form.get("enrollment_count") or 0) or None,
                    (request.form.get("policy_url") or "").strip() or None,
                    int(request.form.get("priority") or 100),
                    int(request.form.get("effective_year") or 2026),
                    policy_id,
                ),
            )
            conn.commit()
            return redirect(url_for("admin_policies_list", notice="政策学校已更新", notice_type="success"))
    finally:
        conn.close()
    return render_template_string(
        ADMIN_POLICY_FORM_HTML,
        policy=policy,
        action_url=f"/admin/policies/{policy_id}/edit",
        school_types=_POLICY_SCHOOL_TYPES,
        target_stages=_POLICY_TARGET_STAGES,
        notice=request.args.get("notice", ""),
        notice_type=request.args.get("notice_type", "error"),
    )


@app.route("/admin/policies/<int:policy_id>/delete", methods=["POST"])
def admin_policies_delete(policy_id: int):
    auth_redirect = require_admin_auth()
    if auth_redirect is not None:
        return auth_redirect
    from task_store import _get_conn
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM policy_match_schools WHERE id = ?", (policy_id,))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("admin_policies_list", notice="政策学校已删除", notice_type="success"))


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
            province=form.get("province", ""),  # v3.8 · 省份
            gender=form["gender"],
            birth_date=form["birth_date"] or None,
            is_minor=is_minor,
            registered_via="self_web" if not form["wechat_openid"] else "wechat",
            note=note,
        )
    except Exception as e:  # noqa: BLE001
        return render_template_string(REGISTER_HTML, cities=CITIES_REGISTRATION, grades=GRADES_REGISTRATION, error=f"注册失败：{e}", form=form)

    # v3.9.6 · 注册成功立即写会话（180 天）· 后续访问 /me、/ 首页自动识别身份
    try:
        _set_student_session(form["luogu_uid"], int(sid), form["real_name"])
    except Exception as _se:
        app.logger.warning(f"[register_student] _set_student_session 失败: {_se}")

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
{{ app_skin_head() }}
</head>
<body class="app-body min-h-screen flex items-center justify-center p-4">
  <div class="app-card max-w-md w-full space-y-4">
    <div class="text-center">
      <div class="text-4xl mb-1">👋</div>
      <h1 class="app-title">进入个人中心</h1>
      <p class="app-subtitle">输入你的洛谷 UID，查看 3 版本学习报告（学员·家长·教练）</p>
    </div>

    <form id="meForm" action="/me/0" method="get" class="space-y-3"
          onsubmit="event.preventDefault();
                    var u=document.getElementById('meUid').value.trim();
                    if(!/^\\d{6,10}$/.test(u)){alert('请输入 6-10 位洛谷 UID');return;}
                    window.location.href='/me/'+u;">
      <label class="app-label">洛谷 UID</label>
      <input id="meUid" type="text" inputmode="numeric" pattern="\\d{6,10}"
             placeholder="如：582694（6-10 位数字）"
             class="app-input" autofocus required>
      <button type="submit" class="app-btn app-btn-primary">
        进入个人中心 →
      </button>
    </form>

    <div class="border-t border-gray-200 pt-3 text-xs text-gray-500 space-y-1">
      <p>💡 不知道自己的 UID？</p>
      <ul class="list-disc list-inside space-y-0.5 text-gray-600">
        <li>登录 <a href="https://www.luogu.com.cn" target="_blank" class="app-link">luogu.com.cn</a>，点右上角头像，URL 里的数字就是 UID</li>
        <li>还没生成报告？<a href="/" class="app-link">先去生成 →</a></li>
      </ul>
    </div>
  </div>
</body>
</html>
"""


@app.route("/me", methods=["GET"])
def me_root():
    """v3.9.6 · /me（无 UID）智能入口：session 有已登录学员 → 自动跳 /me/<uid>；
    否则跳到 /me/ 输入中转页。"""
    try:
        session_uid = str(session.get("student_uid") or "").strip()
        if session_uid and session_uid.isdigit():
            return redirect(url_for("student_me", luogu_uid=session_uid))
    except Exception:
        pass
    return redirect(url_for("me_picker"))


@app.route("/studymate/ai-tutor", methods=["GET", "POST"])
def studymate_ai_tutor():
    """v3.6 · StudyMate AI 讲题入口

    接收来自 /me/<uid> 错题集的「AI 讲题」按钮：
      - GET  ?uid=&pid=&title=&source=&summary=  → 展示该错题 + 「开始 AI 讲题」表单
      - POST （带 problem_id）→ 启动 StudyMate 讲题（v3.6 stub：渲染「AI 正在生成专属题解」占位页，
        后续接 LLM 时把 prompt + problem_id 交给 StudyMate worker 即可）

    设计目标：
      1) 把错题信息（题号 / 标题 / 来源 / AI 摘要 / 错因）打包好交给 StudyMate
      2) 家长订阅门控：未订阅 → 引导去 /redeem
      3) 生成的讲题结果落盘到 reports/<uid>/studymate/<pid>.md（v3.6 暂以 session-only 占位）
    """
    luogu_uid = (request.values.get("uid") or "").strip()
    problem_id = (request.values.get("pid") or "").strip()
    title = (request.values.get("title") or "").strip()
    prob_source = (request.values.get("source") or "").strip()
    summary = (request.values.get("summary") or "").strip()

    if not luogu_uid or not problem_id:
        return render_template_string(
            STUDYMATE_TUTOR_HTML,
            error="缺少必要参数：uid / pid",
            luogu_uid=luogu_uid,
            problem_id=problem_id,
            title=title,
            prob_source=prob_source,
            summary=summary,
            has_parent_sub=False,
            status="error",
        ), 400

    # 家长订阅门控（与 /me/<uid> 同源逻辑）
    has_parent_sub = False
    try:
        from task_store import _get_conn
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM activation_codes ac "
                "JOIN students s ON s.id = ac.student_id "
                "WHERE ac.sku IN ('parent_sub', 'parent_invite') AND s.luogu_uid = ? "
                "AND ac.redeemed_at IS NOT NULL "
                "AND (ac.expires_at IS NULL OR ac.expires_at > datetime('now'))",
                (str(luogu_uid).strip(),),
            ).fetchone()
        finally:
            conn.close()
        has_parent_sub = bool(row and dict(row).get("n", 0) > 0)
    except Exception:
        has_parent_sub = False

    # POST：启动 AI 讲题（v3.6 stub：渲染进度页）
    if request.method == "POST":
        return render_template_string(
            STUDYMATE_TUTOR_HTML,
            error=None,
            luogu_uid=luogu_uid,
            problem_id=problem_id,
            title=title,
            prob_source=prob_source,
            summary=summary,
            has_parent_sub=has_parent_sub,
            status="starting",
        )

    return render_template_string(
        STUDYMATE_TUTOR_HTML,
        error=None,
        luogu_uid=luogu_uid,
        problem_id=problem_id,
        title=title,
        prob_source=prob_source,
        summary=summary,
        has_parent_sub=has_parent_sub,
        status="idle",
    )


STUDYMATE_TUTOR_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>StudyMate AI 讲题 · {{ problem_id or "—" }} · UID {{ luogu_uid }}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body{background:linear-gradient(135deg,#eff6ff 0%,#ecfeff 100%);min-height:100vh;font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;}
        .card-shadow{box-shadow:0 10px 25px rgba(0,0,0,.06);}
        .pulse-dot{width:8px;height:8px;border-radius:50%;background:#3b82f6;display:inline-block;animation:pulse 1.2s infinite;}
        @keyframes pulse{0%,100%{opacity:1;transform:scale(1);}50%{opacity:.4;transform:scale(1.4);}}
        .lock-box{background:linear-gradient(135deg,#fef3c7 0%,#fde68a 100%);border:1px solid #f59e0b;}
    </style>
</head>
<body class="p-4">
<div class="max-w-2xl mx-auto py-6 space-y-4">

    <!-- 顶部 -->
    <div class="bg-white rounded-2xl card-shadow p-5">
        <div class="flex items-center gap-2 mb-2 flex-wrap">
            <span class="text-xs px-2 py-0.5 rounded-full bg-blue-100 text-blue-700 font-bold">🤖 StudyMate</span>
            <span class="text-xs px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-700 font-bold">AI 讲题</span>
            {% if has_parent_sub %}<span class="text-xs px-2 py-0.5 rounded-full bg-green-100 text-green-700 font-bold">✅ 已解锁</span>{% else %}<span class="text-xs px-2 py-0.5 rounded-full bg-amber-100 text-amber-700 font-bold">🔒 需家长订阅</span>{% endif %}
        </div>
        <h1 class="text-2xl font-extrabold text-gray-800">📖 {{ problem_id or "—" }} · {{ title or "(无标题)" }}</h1>
        <p class="text-sm text-gray-500 mt-1">{% if prob_source %}来源：{{ prob_source }} · {% endif %}学员 UID {{ luogu_uid }}</p>
    </div>

    {% if error %}
    <div class="bg-red-50 border border-red-200 rounded-2xl card-shadow p-5 text-sm text-red-700">
        ❌ {{ error }}
        <div class="mt-2"><a href="/me/{{ luogu_uid }}" class="text-red-700 underline">← 返回个人中心</a></div>
    </div>
    {% endif %}

    <!-- 错题上下文（已自动传入） -->
    <div class="bg-white rounded-2xl card-shadow p-5">
        <h2 class="text-sm font-bold text-gray-700 mb-2">📦 已传给 StudyMate 的错题上下文</h2>
        <div class="space-y-2 text-sm">
            <div class="flex gap-2"><span class="text-gray-500 w-16">题号</span><span class="font-mono font-bold text-blue-700">{{ problem_id or "—" }}</span></div>
            <div class="flex gap-2"><span class="text-gray-500 w-16">标题</span><span class="text-gray-800">{{ title or "—" }}</span></div>
            {% if prob_source %}<div class="flex gap-2"><span class="text-gray-500 w-16">来源</span><span class="text-purple-700">{{ prob_source }}</span></div>{% endif %}
            {% if summary %}
            <div class="bg-gray-50 border border-gray-200 rounded-lg p-3 mt-2">
                <div class="text-xs text-gray-500 mb-1">AI 题解摘要</div>
                <div class="text-sm text-gray-700 leading-relaxed">{{ summary }}</div>
            </div>
            {% endif %}
        </div>
    </div>

    {% if not has_parent_sub %}
    <!-- 家长订阅门控 -->
    <div class="lock-box rounded-2xl card-shadow p-5">
        <h2 class="text-base font-bold text-amber-800 mb-2">🔒 AI 讲题 · 需家长订阅</h2>
        <p class="text-sm text-amber-900 mb-3 leading-relaxed">StudyMate AI 讲题是「家长订阅」会员功能。家长加 V 兑换 <code class="font-mono bg-white px-1.5 py-0.5 rounded text-xs">PARENT-SUB-XXXX</code> 后，AI 讲题自动解锁。</p>
        <a href="/redeem" class="inline-block px-4 py-2 bg-amber-500 text-white text-sm font-bold rounded-lg hover:bg-amber-600">🎁 兑换家长订阅码</a>
        <a href="/me/{{ luogu_uid }}" class="inline-block px-4 py-2 border border-amber-300 text-amber-800 text-sm font-bold rounded-lg hover:bg-amber-50 ml-1">← 返回个人中心</a>
    </div>
    {% else %}
    <!-- 启动 / 进度 -->
    <div class="bg-white rounded-2xl card-shadow p-5">
        {% if status == "starting" %}
        <h2 class="text-base font-bold text-blue-700 mb-2 flex items-center gap-2">
            <span class="pulse-dot"></span> AI 正在为你生成专属题解…
        </h2>
        <p class="text-sm text-gray-600 leading-relaxed mb-3">
            StudyMate 正在基于学员 UID <code class="font-mono">{{ luogu_uid }}</code> 的 <b>知识点盲区 / 错题历史 / 提交行为</b>，
            为 <b>{{ problem_id }} {{ title }}</b> 生成「从暴力到正解」的专属讲解（v3.6 接入真实 LLM 后即时返回）。
        </p>
        <div class="bg-blue-50 border border-blue-200 rounded-lg p-3 text-xs text-blue-800">
            <div class="font-bold mb-1">📋 讲题 Prompt 已组装：</div>
            <pre class="whitespace-pre-wrap font-mono text-[11px] leading-relaxed mt-1">题目：{{ problem_id }} {{ title }}{% if prob_source %}（{{ prob_source }}）{% endif %}

学员背景：
- UID: {{ luogu_uid }}
- 错因摘要：{{ summary or "(无)" }}

请生成：
1. 暴力思路（30% 估分）
2. 瓶颈分析（时间/空间）
3. 关键性质/不变量观察
4. 正解代码（C++/Python 均可）
5. 与本学员能力短板关联的训练建议</pre>
        </div>
        <div class="mt-3 text-xs text-gray-500">v3.6 stub：StudyMate 真实讲题 worker 待接入；当前已展示完整 prompt，便于对接。</div>
        {% else %}
        <h2 class="text-base font-bold text-gray-800 mb-2">🚀 准备启动 AI 讲题</h2>
        <p class="text-sm text-gray-600 mb-3">点击下方按钮，StudyMate 将基于该错题上下文生成「从暴力到正解」的专属讲解。</p>
        <form method="POST" action="/studymate/ai-tutor">
            <input type="hidden" name="uid" value="{{ luogu_uid }}">
            <input type="hidden" name="pid" value="{{ problem_id }}">
            <input type="hidden" name="title" value="{{ title }}">
            <input type="hidden" name="source" value="{{ prob_source }}">
            <input type="hidden" name="summary" value="{{ summary }}">
            <button type="submit" class="w-full py-3 bg-gradient-to-r from-blue-500 to-cyan-500 text-white font-bold rounded-lg hover:from-blue-600 hover:to-cyan-600">
                🤖 开始 AI 讲题
            </button>
        </form>
        {% endif %}
    </div>
    {% endif %}

    <div class="text-center text-xs text-gray-400 space-y-1">
        <div><a href="/me/{{ luogu_uid }}" class="text-emerald-600 hover:underline">← 返回个人中心</a></div>
        <div>信竞 AI 报告 · StudyMate v3.6</div>
    </div>
</div>
</body>
</html>
"""


@app.route("/me/", methods=["GET"])
def me_picker():
    """无 UID 的 /me 入口 → UID 输入中转页（避免 404 误判系统故障）

    v3.9.6 · 增强：若 session 里有已登录学员（180 天 cookie）→ 自动跳到 /me/<uid>，
    免去用户每次重新输入 UID 的麻烦。
    """
    try:
        session_uid = str(session.get("student_uid") or "").strip()
        if session_uid and session_uid.isdigit():
            return redirect(url_for("student_me", luogu_uid=session_uid))
    except Exception:
        pass
    return _ME_PICKER_HTML


@app.route("/me/<luogu_uid>")
def student_me(luogu_uid: str):
    """v3.5.2 学员 Pro 自助面板（无密码，仅凭 luogu_uid 进入）

    简化模式：v3.5.2 暂用 luogu_uid 直链（家长端 token 同款模式）。
    未来 v3.5.3 接微信扫码/手机 OTP 后改为带签名 token。

    v3.6 · fallback：未注册但有 report.md → 渲染「轻量版个人中心」（仅展示 UID +
    6 维评分 + 错题集 + AI 讲题入口），不再 404。这让老用户「凭 UID 看错题」成为可能。

    v3.9.6 · 自动续期会话：每次进入都刷新 student_uid / student_name session，
    保证 180 天内不会"掉登录"。

    v3.9.18 · 5 项关键修复：
      1) _find_latest_report_dir 允许 data_only 目录（无 report.md）也能返回
      2) _extract_achievements_from_export_data 修正 failed_items 结构
         （m.problem.pid / m.problem.title 而非 m.pid / m.title）
      3) GESP 兜底加第三层（gesp_exams 表查询），视图层完成避免模板复杂
      4) data_only 状态的历史报告增加「📊 数据预览」入口（/me/<uid>/report-data/<dir>）
      5) lite 版个人中心也展示历史报告（不再仅依赖主路径）
    """
    import sys
    print(f"[DEBUG student_me] uid={luogu_uid!r}", file=sys.stderr, flush=True)
    student = _admin_students.get_student_by_uid(luogu_uid)
    print(f"[DEBUG student_me] get_student_by_uid result: {student is not None}, keys={list(student.keys()) if student else None}", file=sys.stderr, flush=True)

    # v3.9.6 · 同步会话：UID 匹配上 → 续期 session
    if student:
        try:
            _existing_uid = str(session.get("student_uid") or "").strip()
            if _existing_uid != str(luogu_uid).strip():
                # UID 切换了 → 重新写 session
                _set_student_session(
                    str(luogu_uid).strip(),
                    int(student["id"]),
                    (student.get("real_name") or "").strip(),
                )
            else:
                # 同 UID 只刷新时间（保持 180 天活跃）
                session.permanent = True
        except Exception as _se:
            app.logger.debug(f"[student_me] session refresh: {_se}")

    if not student:
        # v3.6 fallback：扫 reports/ 看有没有该 UID 的报告
        from pathlib import Path as _P_fb
        _has_report = False
        try:
            _reports_root = _P_fb(__file__).parent / "reports"
            if _reports_root.exists():
                for _d in _reports_root.iterdir():
                    if not _d.is_dir():
                        continue
                    # v3.9.18 · 放宽：report.md 或 export_data.json 任一即可
                    if not (_d / "report.md").exists() and not (_d / "export_data.json").exists():
                        continue
                    _sidecar = _d / "luogu_uid.txt"
                    if _sidecar.exists():
                        try:
                            if _sidecar.read_text(encoding="utf-8", errors="replace").strip() == str(luogu_uid).strip():
                                _has_report = True
                                break
                        except Exception:
                            pass
                    if str(luogu_uid) in _d.name:
                        _has_report = True
                        break
        except Exception:
            _has_report = False
        if not _has_report:
            return render_template_string(REGISTER_INVALID_HTML, message=f"洛谷 UID {luogu_uid} 未注册"), 404
        # 有 report → 渲染轻量版（用 STUDENT_ME_HTML + 空 student dict）
        return _render_student_me_lite(luogu_uid)
    progress = _admin_students.get_student_gesp_progress(int(student["id"])) or {}
    # v3.9.18 · GESP 第三层兜底：学生表的 gesp_highest_passed/gesp_latest_score 可能为 0
    # （用户自录后没重算 / 注册前在 gesp_exams 表里有记录但 students 表未更新），
    # 直接查 gesp_exams 表兜底，确保 /me 页 GESP 段位永远有值。
    try:
        _gh = int(student.get("gesp_highest_passed") or 0)
        _gs = int(student.get("gesp_latest_score") or 0)
        if not _gh:
            from task_store import _get_conn as _gconn
            _gc = _gconn()
            try:
                _gr = _gc.execute(
                    "SELECT MAX(registered_level) AS lvl, MAX(actual_score) AS sc "
                    "FROM gesp_exams WHERE student_id=? AND passed=1",
                    (int(student["id"]),),
                ).fetchone()
                if _gr and _gr["lvl"]:
                    student_dict_gesp_level = int(_gr["lvl"])
                    student_dict_gesp_score = int(_gr["sc"] or 0)
                else:
                    student_dict_gesp_level = _gh
                    student_dict_gesp_score = _gs
            finally:
                _gc.close()
        else:
            student_dict_gesp_level = _gh
            student_dict_gesp_score = _gs
    except Exception:
        student_dict_gesp_level = int(student.get("gesp_highest_passed") or 0)
        student_dict_gesp_score = int(student.get("gesp_latest_score") or 0)
    # v3.5.2: 解析 city 所在省份 + grade 中文 label
    student_dict = dict(student)
    student_dict["province"] = _city_to_province(student_dict.get("city"))
    student_dict["grade_label"] = _grade_to_label(student_dict.get("grade"))
    # v3.9.18 · 把 GESP 兜底结果显式写回 student_dict（覆盖原 0 值），让模板直读
    student_dict["gesp_highest_passed"] = student_dict_gesp_level
    student_dict["gesp_latest_score"] = student_dict_gesp_score
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
                "WHERE ac.sku IN ('parent_sub', 'parent_invite') AND s.luogu_uid = ? "
                "AND ac.redeemed_at IS NOT NULL "
                "AND (ac.expires_at IS NULL OR ac.expires_at > datetime('now'))",
                (str(luogu_uid).strip(),),
            ).fetchone()
        finally:
            conn.close()
        has_parent_sub = bool(row and dict(row).get("n", 0) > 0)
    except Exception:
        has_parent_sub = False
    # v3.6 · 解析该选手最新报告 → 6 维评分 + 千分制 + 错题
    achievements = {
        "six_dim": {},
        "ai_score_thousand": None,
        "ai_score_label": "—",
        "mistakes": [],
        "report_dir": None,
        "is_partial": False,  # v3.9.17 · 标记是否"半完成"（export_data 完整，AI 报告未生成）
        # v3.9.25 · 新增数据来源追踪（用于精细化 AI 评测分 label 警示语）
        "six_dim_source": None,  # "report_md" / "export_data" / "computed"
        "ai_score_source": None,  # "report_md" / "export_data" / "computed_mean"
    }
    # v3.9.3 · 把 name 传给 _find_latest_report_dir，让"目录名以 _姓名 结尾"兜底分支生效
    try:
        latest = _find_latest_report_dir(luogu_uid, (student.get("real_name") or "") if student else "")
        if latest and (latest / "report.md").exists():
            report_md = (latest / "report.md").read_text(encoding="utf-8", errors="replace")
            if len(report_md.strip()) > 100:  # v3.9.17 · 报告非空
                ext = _extract_achievements_from_report(report_md)
                achievements.update(ext)
                achievements["report_dir"] = latest.name
                # v3.9.25 · 标记 6 维来源：先按 report.md 算
                if ext.get("six_dim"):
                    achievements["six_dim_source"] = "report_md"
                if ext.get("ai_score_thousand"):
                    achievements["ai_score_source"] = "report_md"
                # v3.9.19 · report.md 读到了但 6 维/错题为空 → 兜底 export_data.json
                # v3.9.22 · 改成"逐字段"补全：之前是"6 维+错题都空才触发"，但 report.md 经常
                #   有错题没 6 维（或反之），导致漏 6 维时一直空白。
                # 原因：AI 报告格式漂移、prompt 改了导致提取不到。export_data 是数据源，最权威。
                if (latest / "export_data.json").exists():
                    _ext_fb = _extract_achievements_from_export_data(latest)
                    # 只补缺失字段（已从 report.md 读到的优先保留）
                    if not ext.get("six_dim") and _ext_fb.get("six_dim"):
                        achievements["six_dim"] = _ext_fb["six_dim"]
                        achievements["six_dim_source"] = "export_data"  # v3.9.25 · 标记兜底来源
                    if not ext.get("mistakes") and _ext_fb.get("mistakes"):
                        achievements["mistakes"] = _ext_fb["mistakes"]
                    if not ext.get("ai_score_thousand") and _ext_fb.get("ai_score_thousand"):
                        achievements["ai_score_thousand"] = _ext_fb["ai_score_thousand"]
                        achievements["ai_score_source"] = "export_data"  # v3.9.25 · 标记兜底来源
                        # v3.9.25 · label 文案区分"6 维来源"：
                        #   - 6 维来自 report.md → "AI 报告 6 维已抽取，AI 评分兜底（6 维均值 × 10）"
                        #   - 6 维来自 export_data → "AI 报告 6 维未提取，AI 评分兜底自 export_data"
                        if achievements["six_dim_source"] == "report_md":
                            _mean = sum(achievements["six_dim"].values()) / len(achievements["six_dim"])
                            achievements["ai_score_label"] = f"预估 {int(round(_mean * 10))}/1000（AI 报告 6 维已抽取；评分由 6 维均值 × 10 兜底）"
                        else:
                            achievements["ai_score_label"] = f"预估 {_ext_fb['ai_score_thousand']}/1000（AI 报告 6 维 regex 未匹配，评分来自 export_data.json）"
                    # v3.9.25 · is_partial 改"只对纯 export_data 兜底"为 True。
                    # 6 维来自 report.md 时，即使 AI 评分是 export_data 兜底，也只是补缺，
                    # 不应触发「AI 报告未生成」这种误导性警示。
                    if (not ext.get("six_dim") and not ext.get("mistakes")):
                        # report.md 6 维 + 错题都缺 → AI 报告基本没数据 → 走纯 export_data
                        achievements["is_partial"] = True
            else:
                # v3.9.17 · report.md 是 0 字节（AI 失败），从 export_data.json 兜底
                ext = _extract_achievements_from_export_data(latest)
                achievements.update(ext)
                achievements["report_dir"] = latest.name
                achievements["is_partial"] = True
                if ext.get("six_dim"):
                    achievements["six_dim_source"] = "export_data"
                if ext.get("ai_score_thousand"):
                    achievements["ai_score_source"] = "export_data"
        elif latest and (latest / "export_data.json").exists():
            # v3.9.17 · 没有 report.md 但有 export_data.json
            ext = _extract_achievements_from_export_data(latest)
            achievements.update(ext)
            achievements["report_dir"] = latest.name
            achievements["is_partial"] = True
            if ext.get("six_dim"):
                achievements["six_dim_source"] = "export_data"
            if ext.get("ai_score_thousand"):
                achievements["ai_score_source"] = "export_data"
    except Exception as _e:
        achievements["_err"] = str(_e)[:200]

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
        achievements=achievements,
        mistake_count=len(achievements.get("mistakes") or []),
        # v3.9.7 · 历史报告列表（从原 /report/student 页面合并过来）
        report_htmls=_list_student_report_htmls(luogu_uid, (student.get("real_name") or "") if student else "", limit=8),
    )


@app.route("/me/<luogu_uid>/report-data/<dir_name>")
def student_me_report_data(luogu_uid: str, dir_name: str):
    """v3.9.18 · 半完成报告（data_only）的数据预览：6 维评分 + 错题 + 抓题概况。

    学员点「📊 数据预览」按钮直达这里。直接读 export_data.json 渲染，
    不依赖 report.md（AI 失败时就是 0 字节）。
    """
    # 安全检查：dir_name 必须是 8 字符 hex 开头（task_id 短哈希），防止路径穿越
    if "/" in dir_name or "\\" in dir_name or ".." in dir_name:
        return "Invalid dir name", 400
    if not (dir_name[:8].isalnum() and len(dir_name) >= 8):
        return "Invalid dir name", 400
    reports_root = _ROOT / "reports"
    report_dir = reports_root / dir_name
    if not report_dir.exists() or not (report_dir / "export_data.json").exists():
        return render_template_string(REGISTER_INVALID_HTML, message="报告目录不存在或已删除"), 404
    # 校验该 dir 与该 uid 匹配
    try:
        _sidecar = report_dir / "luogu_uid.txt"
        if _sidecar.exists():
            if _sidecar.read_text(encoding="utf-8", errors="replace").strip() != str(luogu_uid).strip():
                return render_template_string(REGISTER_INVALID_HTML, message="无权限查看该报告"), 403
        elif str(luogu_uid) not in dir_name:
            return render_template_string(REGISTER_INVALID_HTML, message="无权限查看该报告"), 403
    except Exception:
        pass
    # 提取 6 维 + 错题 + 抓题概况
    ach = _extract_achievements_from_export_data(report_dir)
    try:
        with open(report_dir / "export_data.json", "r", encoding="utf-8") as fp:
            d = json.load(fp)
    except Exception as _e:
        return f"export_data.json 解析失败: {_e}", 500
    solved = d.get("solved_count", 0)
    failed = d.get("failed_count", 0)
    student_info = d.get("student_info") or {}
    eval_time = (student_info.get("eval_time") or "").strip()
    return render_template_string(
        _REPORT_DATA_PREVIEW_HTML,
        dir_name=dir_name,
        luogu_uid=luogu_uid,
        achievements=ach,
        solved=solved,
        failed=failed,
        total=solved + failed,
        eval_time=eval_time,
    )


_REPORT_DATA_PREVIEW_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>数据预览 · {{ dir_name }} · v3.9.18</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        .preview-hero{background:linear-gradient(135deg,#f59e0b 0%,#ef4444 100%);color:#fff;border-radius:16px;padding:20px;}
    </style>
</head>
<body class="bg-gradient-to-br from-amber-50 to-rose-50 min-h-screen p-4">
    <div class="max-w-3xl mx-auto">
        <div class="preview-hero mb-4">
            <h1 class="text-2xl font-extrabold mb-1">📊 数据预览</h1>
            <p class="text-sm opacity-90">UID <strong>{{ luogu_uid }}</strong> · {{ dir_name }}</p>
            <p class="text-xs opacity-75 mt-1">⚠️ AI 报告未生成 · 以下数据来自 export_data.json 练习阶段</p>
        </div>

        <div class="bg-white rounded-2xl shadow p-5 mb-4">
            <h2 class="text-lg font-bold text-gray-800 mb-3">🎯 抓题概况</h2>
            <div class="grid grid-cols-3 gap-3 text-center">
                <div class="bg-emerald-50 rounded-xl p-3">
                    <div class="text-xs text-emerald-700 font-bold">AC 通过</div>
                    <div class="text-3xl font-extrabold text-emerald-700 mt-1">{{ solved }}</div>
                </div>
                <div class="bg-rose-50 rounded-xl p-3">
                    <div class="text-xs text-rose-700 font-bold">失败</div>
                    <div class="text-3xl font-extrabold text-rose-700 mt-1">{{ failed }}</div>
                </div>
                <div class="bg-blue-50 rounded-xl p-3">
                    <div class="text-xs text-blue-700 font-bold">总计</div>
                    <div class="text-3xl font-extrabold text-blue-700 mt-1">{{ total }}</div>
                </div>
            </div>
            {% if eval_time %}
            <div class="text-[11px] text-gray-400 mt-3">抓题时间：{{ eval_time }}</div>
            {% endif %}
        </div>

        <div class="bg-white rounded-2xl shadow p-5 mb-4">
            <h2 class="text-lg font-bold text-gray-800 mb-3">📊 6 维能力评分（练习阶段预估）</h2>
            {% if achievements.six_dim %}
            <div class="space-y-2">
                {% for k, v in achievements.six_dim.items() %}
                <div class="flex items-center gap-2 text-sm">
                    <div class="w-20 text-gray-600 text-right">{{ k }}</div>
                    <div class="flex-1 bg-gray-100 rounded-full h-3 overflow-hidden">
                        <div class="h-full rounded-full
                            {% if v >= 75 %}bg-green-500
                            {% elif v >= 55 %}bg-emerald-400
                            {% elif v >= 40 %}bg-amber-400
                            {% else %}bg-red-400{% endif %}" style="width: {{ v }}%"></div>
                    </div>
                    <div class="w-10 text-right font-mono font-bold
                        {% if v >= 75 %}text-green-700
                        {% elif v >= 55 %}text-emerald-700
                        {% elif v >= 40 %}text-amber-700
                        {% else %}text-red-700{% endif %}">{{ v }}</div>
                </div>
                {% endfor %}
            </div>
            <div class="text-[11px] text-gray-400 mt-3">
                千分制预估：<strong>{{ achievements.ai_score_thousand }}/1000</strong>（来自 6 维均分 ×10，AI 报告未生成时使用）
            </div>
            {% else %}
            <div class="text-sm text-gray-400">暂未抓到 6 维评分</div>
            {% endif %}
        </div>

        <div class="bg-white rounded-2xl shadow p-5 mb-4">
            <h2 class="text-lg font-bold text-gray-800 mb-3">❌ 错题预览（前 5）</h2>
            {% if achievements.mistakes %}
            <div class="space-y-2">
                {% for m in achievements.mistakes %}
                <div class="border border-gray-200 rounded-lg p-3">
                    <div class="font-bold text-sm text-gray-800">{{ m.idx }}. {{ m.title }}</div>
                    <div class="text-[11px] text-gray-400 mt-1">
                        {{ m.problem_id }} · {{ m.summary }}
                    </div>
                </div>
                {% endfor %}
            </div>
            {% else %}
            <div class="text-sm text-gray-400">无错题</div>
            {% endif %}
        </div>

        <div class="text-center mt-4">
            <a href="/me/{{ luogu_uid }}" class="text-sm text-emerald-700 hover:underline">← 返回个人中心</a>
        </div>
    </div>
</body>
</html>
"""


def _render_student_me_lite(luogu_uid: str):
    """v3.6 · 轻量版个人中心（学员未注册但有 report.md 时回退到这）

    只展示：UID + 6 维评分 + 千分制 + 错题集 + AI 讲题入口
    隐藏：GESP 段位、自录奖项、家长订阅 CTA（这些都需要先注册）
    """
    from pathlib import Path as _P_lite
    # 解析最新 report
    achievements = {
        "six_dim": {},
        "ai_score_thousand": None,
        "ai_score_label": "—",
        "mistakes": [],
        "report_dir": None,
    }
    # v3.9.3 · 把 name 传给 _find_latest_report_dir，让"目录名以 _姓名 结尾"兜底分支生效
    # v3.9.4 · 修复 student 未定义导致 NameError 的 bug（lite 路径无 student 变量）
    try:
        # 先从 students 表查学员名（如果存在）
        _lite_name = ""
        try:
            _stu = _admin_students.get_student_by_uid(luogu_uid) or {}
            _lite_name = (_stu.get("real_name") or "").strip()
        except Exception:
            _lite_name = ""
        latest = _find_latest_report_dir(luogu_uid, _lite_name)
        if latest and (latest / "report.md").exists():
            report_md = (latest / "report.md").read_text(encoding="utf-8", errors="replace")
            if len(report_md.strip()) > 100:
                ext = _extract_achievements_from_report(report_md)
                achievements.update(ext)
                achievements["report_dir"] = latest.name
                # v3.9.25 · 标记 6 维/AI 评分来源（与主路径保持一致）
                if ext.get("six_dim"):
                    achievements["six_dim_source"] = "report_md"
                if ext.get("ai_score_thousand"):
                    achievements["ai_score_source"] = "report_md"
                # v3.9.22 · 逐字段补全（与主路径保持一致）：report.md 没提取到 6 维/错题时
                # 用 export_data.json 兜底。
                if (latest / "export_data.json").exists():
                    _ext_fb = _extract_achievements_from_export_data(latest)
                    if not ext.get("six_dim") and _ext_fb.get("six_dim"):
                        achievements["six_dim"] = _ext_fb["six_dim"]
                        achievements["six_dim_source"] = "export_data"
                    if not ext.get("mistakes") and _ext_fb.get("mistakes"):
                        achievements["mistakes"] = _ext_fb["mistakes"]
                    if not ext.get("ai_score_thousand") and _ext_fb.get("ai_score_thousand"):
                        achievements["ai_score_thousand"] = _ext_fb["ai_score_thousand"]
                        achievements["ai_score_source"] = "export_data"
                        # v3.9.25 · label 文案区分"6 维来源"
                        if achievements["six_dim_source"] == "report_md":
                            _mean = sum(achievements["six_dim"].values()) / len(achievements["six_dim"])
                            achievements["ai_score_label"] = f"预估 {int(round(_mean * 10))}/1000（AI 报告 6 维已抽取；评分由 6 维均值 × 10 兜底）"
                        else:
                            achievements["ai_score_label"] = f"预估 {_ext_fb['ai_score_thousand']}/1000（AI 报告 6 维 regex 未匹配，评分来自 export_data.json）"
                    # v3.9.25 · 与主路径一致：6 维 + 错题都缺才标 partial
                    if (not ext.get("six_dim") and not ext.get("mistakes")):
                        achievements["is_partial"] = True
            else:
                # v3.9.18 · report.md 是 0 字节（AI 失败）→ 兜底 export_data.json
                ext = _extract_achievements_from_export_data(latest)
                achievements.update(ext)
                achievements["report_dir"] = latest.name
                achievements["is_partial"] = True
                if ext.get("six_dim"):
                    achievements["six_dim_source"] = "export_data"
                if ext.get("ai_score_thousand"):
                    achievements["ai_score_source"] = "export_data"
        elif latest and (latest / "export_data.json").exists():
            # v3.9.18 · 没有 report.md 但有 export_data.json → 兜底
            ext = _extract_achievements_from_export_data(latest)
            achievements.update(ext)
            achievements["report_dir"] = latest.name
            achievements["is_partial"] = True
            if ext.get("six_dim"):
                achievements["six_dim_source"] = "export_data"
            if ext.get("ai_score_thousand"):
                achievements["ai_score_source"] = "export_data"
    except Exception as _e:
        achievements["_err"] = str(_e)[:200]

    # 构造一个匿名 student dict（让模板不报 KeyError）
    student_dict = {
        "real_name": None,
        "luogu_uid": luogu_uid,
        "province": None,
        "city": None,
        "gender": None,
        "grade": None,
        "grade_label": None,
        "registered_via": "report-only",
    }
    # v3.9.18 · 也传历史报告列表（lite 版学员虽未注册，但仍能看到历史报告入口）
    try:
        _report_htmls = _list_student_report_htmls(luogu_uid, _lite_name, limit=8)
    except Exception:
        _report_htmls = []
    return render_template_string(
        STUDENT_ME_LITE_HTML,
        student=student_dict,
        token=luogu_uid,
        achievements=achievements,
        mistake_count=len(achievements.get("mistakes") or []),
        report_htmls=_report_htmls,
    )


# v3.6 · 轻量版个人中心模板（只展示 UID + 6 维 + 错题 + AI 讲题）
STUDENT_ME_LITE_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>错题本 · UID {{ token }} · v3.6</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gradient-to-br from-emerald-50 to-cyan-50 min-h-screen">
    <div class="bg-gradient-to-r from-emerald-600 to-cyan-600 text-white">
        <div class="max-w-3xl mx-auto p-6">
            <h1 class="text-2xl font-bold mb-1">📚 错题本 · v3.6</h1>
            <p class="text-sm opacity-90">洛谷 UID <strong>{{ token }}</strong> · 仅展示 AI 报告中的错题与个人成就</p>
            <p class="text-xs opacity-75 mt-1">
                💡 学员尚未完成注册，如需 GESP 段位 / 自录奖项 / 家长订阅等功能，
                <a href="/generate-form" class="underline font-bold">🚀 去生成学习报告（含注册）→</a>
            </p>
        </div>
    </div>

    <div class="max-w-3xl mx-auto p-4 -mt-4 space-y-4">

        <!-- 个人成就（千分制 + 6 维） -->
        <div class="bg-white rounded-2xl shadow p-5">
            <h2 class="text-lg font-bold text-gray-800 mb-3">🏅 个人成就（来自 AI 报告）</h2>

            <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
                <div class="bg-gradient-to-br from-amber-50 to-yellow-50 border border-amber-200 rounded-xl p-4 text-center">
                    <div class="text-xs text-amber-700 font-bold">⭐ AI 评测分（千分制）</div>
                    {% if achievements.ai_score_thousand is not none %}
                    <div class="text-4xl font-extrabold text-amber-700 mt-1">{{ achievements.ai_score_thousand }}</div>
                    <div class="text-xs text-amber-600 mt-1 px-1 break-words leading-snug">{{ achievements.ai_score_label }} · 满分 1000</div>
                    {% else %}
                    <div class="text-3xl font-extrabold text-gray-300 mt-1">—</div>
                    <div class="text-xs text-gray-400 mt-1">暂未生成 AI 报告</div>
                    {% endif %}
                </div>
                <div class="bg-gradient-to-br from-gray-50 to-gray-100 border border-gray-200 rounded-xl p-4 text-center">
                    <div class="text-xs text-gray-600 font-bold">🏆 GESP 段位</div>
                    <div class="text-3xl font-extrabold text-gray-300 mt-1">—</div>
                    <div class="text-xs text-gray-400 mt-1">需先注册</div>
                </div>
                <div class="bg-gradient-to-br from-gray-50 to-gray-100 border border-gray-200 rounded-xl p-4 text-center">
                    <div class="text-xs text-gray-600 font-bold">🏅 信息学奖项</div>
                    <div class="text-3xl font-extrabold text-gray-300 mt-1">0</div>
                    <div class="text-xs text-gray-400 mt-1">需先注册</div>
                </div>
            </div>

            {% if achievements.six_dim %}
            <div class="mt-4 border-t border-gray-100 pt-3">
                <div class="text-xs text-gray-500 mb-2">📊 6 维能力评分</div>
                <div class="space-y-1.5">
                    {% for k, v in achievements.six_dim.items() %}
                    <div class="flex items-center gap-2 text-xs">
                        <div class="w-20 text-gray-600 text-right">{{ k }}</div>
                        <div class="flex-1 bg-gray-100 rounded-full h-2.5 overflow-hidden">
                            <div class="h-full rounded-full
                                {% if v >= 75 %}bg-green-500
                                {% elif v >= 55 %}bg-emerald-400
                                {% elif v >= 40 %}bg-amber-400
                                {% else %}bg-red-400{% endif %}" style="width: {{ v }}%"></div>
                        </div>
                        <div class="w-10 text-right font-mono font-bold
                            {% if v >= 75 %}text-green-700
                            {% elif v >= 55 %}text-emerald-700
                            {% elif v >= 40 %}text-amber-700
                            {% else %}text-red-700{% endif %}">{{ v }}</div>
                    </div>
                    {% endfor %}
                </div>
                {% if achievements.report_dir %}
                {# v3.9.28 · 「数据来源」动态显示（lite 版，与主模板一致） #}
                {% set _src6 = achievements.six_dim_source or 'unknown' %}
                {% set _src_ai = achievements.ai_score_source or 'unknown' %}
                {% set _src_text = '' %}
                {% if _src6 == 'report_md' and _src_ai == 'report_md' %}
                    {% set _src_text = 'report.md' %}
                {% elif _src6 == 'report_md' %}
                    {% set _src_text = 'report.md（6 维）+ export_data.json（AI 评分）' %}
                {% elif _src6 == 'export_data' and _src_ai == 'export_data' %}
                    {% set _src_text = 'export_data.json（AI 报告 6 维未识别，回退到结构化数据）' %}
                {% elif _src6 == 'export_data' %}
                    {% set _src_text = 'export_data.json' %}
                {% else %}
                    {% set _src_text = 'report.md / export_data.json（混合）' %}
                {% endif %}
                <div class="text-[10px] text-gray-400 mt-2" title="6 维来源={{ _src6 }}，AI 评分来源={{ _src_ai }}">数据来源：{{ achievements.report_dir }} / {{ _src_text }}</div>
                {% endif %}
            </div>
            {% endif %}
        </div>

        <!-- 错题集 -->
        <div class="bg-white rounded-2xl shadow p-5" id="mistakes-section">
            <div class="flex items-center justify-between mb-3 flex-wrap gap-2">
                <h2 class="text-lg font-bold text-gray-800">📚 我的错题集</h2>
                {# v3.9.19 · 显示"显示 N / 共 M 道"，让用户知道有更多错题可展开 #}
                <span class="text-xs text-gray-500">
                    显示 {{ mistake_count }}{% if achievements.total_mistakes and achievements.total_mistakes > mistake_count %} / 共 {{ achievements.total_mistakes }} 道{% endif %}
                    {% if achievements.report_dir %}· 来自 {{ achievements.report_dir[:8] }}{% endif %}
                </span>
            </div>

            {% if mistake_count > 0 %}
            {# v3.9.19 · 默认显示全部（> 5 道时给个「折叠」按钮），按用户要求：先全部展示再可折叠 #}
            <div id="mistakes-list" class="space-y-2 max-h-[420px] overflow-y-auto pr-1">
                {% for m in achievements.mistakes %}
                <div class="mistake-item border border-gray-200 rounded-lg p-3 hover:border-emerald-300 transition{% if loop.index0 >= 5 %} extra-mistake{% endif %}">
                    <div class="flex items-start justify-between gap-2 flex-wrap">
                        <div class="min-w-0 flex-1">
                            <div class="flex items-center gap-1.5 flex-wrap text-sm">
                                <span class="text-gray-400 font-mono text-xs">#{{ m.idx }}</span>
                                {% if m.problem_id %}
                                <a href="https://www.luogu.com.cn/problem/{{ m.problem_id }}" target="_blank" class="font-bold text-blue-700 hover:underline">{{ m.problem_id }}</a>
                                {% endif %}
                                <span class="font-bold text-gray-800 truncate">{{ m.title }}</span>
                                {% if m.source %}<span class="text-[10px] px-1.5 py-0.5 bg-purple-100 text-purple-700 rounded">{{ m.source }}</span>{% endif %}
                            </div>
                            {% if m.summary %}
                            <div class="text-xs text-gray-600 mt-1.5 line-clamp-2">💡 {{ m.summary }}</div>
                            {% endif %}
                        </div>
                        <!-- v3.9.9 · 直跳 aijiangti.cn · C++ 课件生成（题号/标题/来源已直传） -->
                        <a href="https://aijiangti.cn/?pid={{ m.problem_id }}&from=luogu&lang=cpp&require={{ '用C++代码实现并讲解'|urlencode }}&source={{ (m.source or '')|urlencode }}&title={{ (m.title or '')|urlencode }}"
                           target="_blank" rel="noopener"
                           class="flex-shrink-0 inline-flex items-center gap-1 px-3 py-1.5 bg-gradient-to-r from-blue-500 to-cyan-500 text-white text-xs font-bold rounded-lg hover:from-blue-600 hover:to-cyan-600 whitespace-nowrap"
                           title="跳到 aijiangti.cn 生成 C++ 课件（题目已传入）">
                            🤖 AI 讲题
                        </a>
                    </div>
                </div>
                {% endfor %}
            </div>
            {# v3.9.19 · 「折叠」切换按钮，错题 > 5 道才显示。默认全部展开 #}
            {% if achievements.mistakes|length > 5 %}
            <div class="text-center mt-3">
                <button id="mistakes-toggle-btn" onclick="toggleMistakes()" type="button"
                        class="text-xs px-4 py-1.5 bg-emerald-50 hover:bg-emerald-100 text-emerald-700 rounded-md font-bold">
                    折叠错题（仅看前 5 道）
                </button>
            </div>
            <style>
                #mistakes-list.collapsed .extra-mistake { display: none; }
            </style>
            <script>
                function toggleMistakes() {
                    var list = document.getElementById('mistakes-list');
                    var btn = document.getElementById('mistakes-toggle-btn');
                    if (!list || !btn) return;
                    list.classList.toggle('collapsed');
                    if (list.classList.contains('collapsed')) {
                        btn.textContent = '展开全部错题（{{ achievements.mistakes|length }} 道）';
                    } else {
                        btn.textContent = '折叠错题（仅看前 5 道）';
                    }
                }
            </script>
            {% endif %}
            <p class="text-[10px] text-gray-400 mt-2">💡 点击「AI 讲题」直跳 aijiangti.cn · 自动用 C++ 代码实现并讲解（题目已传入）</p>
            {% else %}
            <div class="text-center py-6 text-sm text-gray-400">
                {% if achievements.is_partial %}
                🌱 最新一份报告 <code class="text-emerald-600">{{ achievements.report_dir }}</code> 未抽取到错题
                {% else %}
                🌱 暂无错题记录 · <a href="/" class="text-emerald-600 hover:underline">去生成新报告 →</a>
                {% endif %}
            </div>
            {% endif %}
        </div>

        <!-- v3.9.18 · 历史报告（lite 版也展示，让未注册学员能查看数据预览） -->
        <div class="bg-white rounded-2xl shadow p-5">
            <div class="flex items-center justify-between mb-3">
                <h2 class="text-lg font-bold text-gray-800">📄 历史报告</h2>
                <span class="text-xs text-gray-400">共 {{ report_htmls|length }} 份</span>
            </div>
            {% if report_htmls %}
            <div class="space-y-2">
                {% for r in report_htmls %}
                <div class="flex items-center justify-between border border-gray-200 rounded-lg p-3 hover:bg-gray-50">
                    <div class="flex-1 min-w-0">
                        <div class="flex items-center gap-2">
                            <span class="text-sm font-bold text-emerald-700">📅 {{ r.mtime_display }}</span>
                            {% if loop.first %}<span class="text-[10px] px-1.5 py-0.5 bg-emerald-100 text-emerald-700 rounded">最新</span>{% endif %}
                            {% if r.status == "data_only" %}
                            <span class="text-[10px] px-1.5 py-0.5 bg-amber-100 text-amber-700 rounded">📦 数据已抓取</span>
                            {% endif %}
                        </div>
                        <div class="text-[11px] text-gray-400 mt-0.5 truncate">{{ r.dir_name }} · {{ r.size_kb }} KB</div>
                    </div>
                    <div class="ml-2">
                        {% if r.status == "complete" and r.html_url %}
                        <a href="{{ r.html_url }}" target="_blank"
                           class="px-2.5 py-1.5 rounded-md bg-emerald-50 hover:bg-emerald-100 text-emerald-700 text-xs font-bold">🔍 查看</a>
                        {% elif r.status == "data_only" %}
                        <a href="/me/{{ token }}/report-data/{{ r.dir_name }}" target="_blank"
                           class="px-2.5 py-1.5 rounded-md bg-amber-50 hover:bg-amber-100 text-amber-700 text-xs font-bold">📊 数据预览</a>
                        {% endif %}
                    </div>
                </div>
                {% endfor %}
            </div>
            {% else %}
            <div class="text-center py-3 text-sm text-gray-400">🌱 暂无历史报告</div>
            {% endif %}
        </div>

        <div class="text-center text-xs text-gray-400">
            信竞 AI 报告 · 错题本 v3.6 ·
            <a href="/" class="text-emerald-600 hover:underline">返回首页</a>
        </div>
    </div>
</body>
</html>
"""


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
        # v3.9 · 合并级别细分：不再显示 "1-4" / "5-6" / "7-8" 这种分段，
        # 统一显示为 "GESP 考级（夏考）" 等更简洁的形式
        m = _re_share.search(r"（[\d\-]+\s*级\s*([春秋夏冬]?考)）", name)
        if m:
            season = m.group(1) or ""
            return f"GESP 考级（{season}）" if season else "GESP 考级"
        return "GESP 考级"
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
        # v3.9 · 新格式（无【】）："### 当前对应等级水平\n**CSP‑J 熟练 → CSP‑S 入门**"
        r"对应等级水平[：:]?\s*\*\*([^*\n]+?)\*\*",             # 行内粗体（如 "**CSP-S 入门阶段**（..."）
        r"对应等级水平[^\n]*\n+\s*\*\*([^*\n]+?)\*\*",          # 跨行粗体（标题后下一行）
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


def _extract_achievements_from_export_data(report_dir) -> dict:
    """v3.9.17 · 从 export_data.json 抽成就（AI 报告失败时的兜底）

    之前 _extract_achievements_from_report 只读 report.md，
    但 report.md 经常 0 字节（AI 401 / 流式中断），导致 /me 页空白。
    export_data.json 是抓题/分析阶段生成的，**总是**完整的，所以用它兜底。

    返回 dict 字段同 _extract_achievements_from_report：
      - six_dim: dict  6 维能力评分（基础算法/数据结构/图论/DP/字符串/数学）
      - ai_score_thousand: int (0-1000) = mean(six_dim) * 10
      - ai_score_label:    str   "预估分（AI 报告未生成）" 标记
      - mistakes: list[dict]   从 failed_items 抽 top 5
    """
    import json as _json_e
    out = {
        "six_dim": {},
        "ai_score_thousand": None,
        "ai_score_label": "—（AI 报告未生成）",
        "mistakes": [],
    }
    try:
        export_path = report_dir / "export_data.json"
        if not export_path.exists():
            return out
        with open(export_path, "r", encoding="utf-8") as fp:
            d = _json_e.load(fp)

        # 1) 6 维评分
        six = d.get("six_dimension_scores") or {}
        if isinstance(six, dict) and six:
            out["six_dim"] = {k: int(v) for k, v in six.items() if isinstance(v, (int, float))}

        # 2) 千分制 = 6 维平均分 * 10
        if out["six_dim"]:
            mean_100 = sum(out["six_dim"].values()) / len(out["six_dim"])
            out["ai_score_thousand"] = int(round(mean_100 * 10))
            out["ai_score_label"] = f"预估 {out['ai_score_thousand']}/1000（练习阶段 · AI 报告未生成）"

        # 1.b) v3.9.24 · 6 维兜底（v3.9.19/22 已修过 report.md 缺 6 维的场景，但
        # export_data.json 自身没 6 维时仍然空白）：
        #   1) 先用 summary 重新跑 compute_six_dimension_scores（如果 summary 完整）
        #   2) 仍空 → 从 failed_items 数量+难度粗略推断（标记 partial 兜底）
        # 目标：图 2 那种「错题 5 道、能力维度 0 维、AI 评测分 —」的页面至少有个数。
        if not out["six_dim"]:
            try:
                from behavior_analyzer import compute_six_dimension_scores as _c6
                _r = _c6(
                    {
                        "solved_count": int(d.get("solved_count") or 0),
                        "summary": d.get("summary") or {},
                    },
                    d.get("behavior_analysis") or {},
                )
                if isinstance(_r, dict) and _r:
                    # 过滤出 6 个核心维度（与 behavior_analyzer 一致）
                    _6keys = ("基础算法", "数据结构", "图论", "动态规划", "字符串", "数学")
                    out["six_dim"] = {k: int(_r.get(k) or 0) for k in _6keys if int(_r.get(k) or 0) > 0}
            except Exception as _e6:
                app.logger.debug(f"[six_dim_fallback_v3924] compute_six_dimension_scores failed: {_e6}")

        if not out["six_dim"]:
            # 终极兜底：完全没数据时给个「练习阶段」基础分（避免显示 0 维）
            # 错题数越少 → 基础分越高（粗略反映掌握度）
            try:
                _fc = len(d.get("failed_items") or [])
                _pc = int(d.get("solved_count") or 0)
                # 基础分 = 50 + 通过数 / (通过数+错题数) * 30，钳到 [40, 80]
                _rate = _pc / max(1, _pc + _fc) if (_pc + _fc) > 0 else 0.5
                _base = int(round(40 + _rate * 30))
                out["six_dim"] = {
                    "基础算法": _base,
                    "数据结构": max(35, _base - 5),
                    "图论": max(35, _base - 8),
                    "动态规划": max(35, _base - 10),
                    "字符串": max(35, _base - 12),
                    "数学": max(35, _base - 15),
                }
            except Exception:
                pass

        # 兜底完成后：补算千分制 + 标签（无论 6 维从哪个分支来，都统一在此重算）
        if out["six_dim"]:
            mean_100 = sum(out["six_dim"].values()) / len(out["six_dim"])
            out["ai_score_thousand"] = int(round(mean_100 * 10))
            if not out.get("ai_score_label") or out["ai_score_label"] == "—（AI 报告未生成）":
                # 6 维来自兜底 → 标「练习阶段」
                out["ai_score_label"] = f"预估 {out['ai_score_thousand']}/1000（练习阶段 · AI 报告未生成）"

        # 3) 错题 - v3.9.19 · 扩大提取数（默认 50 道），让"展开全部"按钮可点
        # 之前只取前 5 道，导致「更多错题」永远只是同一个数字。
        _MAX_MISTAKES = 50
        _all_failed = d.get("failed_items") or []
        for i, m in enumerate(_all_failed[:_MAX_MISTAKES], 1):
            # v3.9.18 · 修复结构：实际是 m.problem.pid / m.problem.title（嵌套对象）
            # 兼容旧结构（m.pid / m.title 直接挂在 top）
            problem_obj = m.get("problem") or {}
            if not isinstance(problem_obj, dict):
                problem_obj = {}
            pid = str(problem_obj.get("pid") or m.get("pid") or m.get("problem_id") or "").strip()
            title = str(problem_obj.get("title") or m.get("title") or "").strip()
            if not title and pid:
                title = pid
            out["mistakes"].append({
                "idx": i,
                "problem_id": pid,
                "title": title[:60] or "(无标题)",
                "source": str(problem_obj.get("tag_type") or m.get("source") or m.get("tag_type") or "")[:30],
                "summary": f"难度 {problem_obj.get('difficulty', m.get('difficulty', '?'))} · AC 失败",
            })
        # v3.9.19 · 实际错题总数（用于"共 N 道"展示），以及 total_mistakes 字段
        out["total_mistakes"] = len(_all_failed)
    except Exception:
        pass
    return out


def _extract_achievements_from_report(report_md: str) -> dict:
    """v3.6 · 从 report.md 抽「个人成就」+「错题集」。

    返回 dict：
      - six_dim:    {"基础算法": 72, "数据结构": 33, "图论": 33,
                    "动态规划": 39, "字符串": 47, "数学": 40}  (0-100)
      - ai_score_thousand: int (0-1000)   = mean(six_dim) * 10
      - ai_score_label:    str           = "⭐⭐⭐⭐" 等档位（5 档）
      - mistakes: list[dict]，每项：
          { idx, problem_id, title, source, summary, bottleneck }
        problem_id 可能是 "P11229" 或 ""（无法识别时）

    v3.9.17 · 抽出 _extract_achievements_from_export_data 作为 AI 失败时的兜底。
    """
    import re
    out = {
        "six_dim": {},
        "ai_score_thousand": None,
        "ai_score_label": "—",
        "mistakes": [],
    }
    if not report_md:
        return out

    # ─── 1) 六维能力评分（第 4 节「六维能力雷达表与诊断」）──
    # 行形如：
    #   | **基础算法** | **72** | 🟡 熟练 | ...   （分数有 **）
    #   | **基础算法** | 90 | 🟢 精通 | ...      （分数无 **）
    dim_keys = ["基础算法", "数据结构", "图论", "动态规划", "字符串", "数学"]
    six = {}
    for k in dim_keys:
        # 宽匹配：行首 | ... 关键字 ... | ... 数字 ... |
        m = re.search(
            r"^\s*\|[\s*]*?" + re.escape(k) + r"[\s*]*?\|[\s*]*?(\d{1,3})[\s*]*?\|",
            report_md, re.M,
        )
        if m:
            try:
                v = int(m.group(1))
                if 0 <= v <= 100:
                    six[k] = v
            except Exception:
                pass
    out["six_dim"] = six

    # ─── 2) 千分制总分 = mean × 10 ───────────────────
    if six:
        mean_score = sum(six.values()) / len(six)
        out["ai_score_thousand"] = round(mean_score * 10)
        # 5 档评级
        s = out["ai_score_thousand"]
        if s >= 850:
            out["ai_score_label"] = "🏆 顶尖"
        elif s >= 700:
            out["ai_score_label"] = "⭐ 优秀"
        elif s >= 550:
            out["ai_score_label"] = "🔵 良好"
        elif s >= 400:
            out["ai_score_label"] = "🟡 基础"
        else:
            out["ai_score_label"] = "🔴 待提升"

    # ─── 3) 错题集（第 10 节「未通过题目专属题解」）──────
    # 锚点：### 10. 【未通过题目...(从暴力到正解)】  → 抓 "未通过题目" 之后整行
    m10 = re.search(
        r"^###\s*10\.?\s*[【\[]未通过题目.*?$",
        report_md, re.M,
    )
    if not m10:
        return out
    section = report_md[m10.end():]

    # 找所有 #### N. xxx 块
    blocks = re.split(r"^####\s*(\d+)\.\s*(.+?)$", section, flags=re.M)
    # blocks 形如 [pre, idx1, title1, body1, idx2, title2, body2, ...]
    mistakes = []
    i = 1
    while i + 2 <= len(blocks) - 1:
        idx = blocks[i].strip()
        title_line = blocks[i + 1].strip()
        body = blocks[i + 2]
        i += 3

        # 解析题号 / 来源 / 标题
        # 形如 "P11229 [CSP-J 2024] 小木棍"
        pid = ""
        source = ""
        title = title_line
        m_pid = re.match(r"(P\d{4,6})\s*(.*)", title_line)
        if m_pid:
            pid = m_pid.group(1)
            rest = m_pid.group(2).strip()
            m_src = re.match(r"\[([^\]]+)\]\s*(.*)", rest)
            if m_src:
                source = m_src.group(1).strip()
                title = m_src.group(2).strip()
            else:
                title = rest
        else:
            # 可能 "P11229 小木棍"（无来源）
            m_pid2 = re.match(r"(P\d{4,6})\s+(.+)", title_line)
            if m_pid2:
                pid = m_pid2.group(1)
                title = m_pid2.group(2).strip()

        # AI 题解摘要
        summary = ""
        m_sum = re.search(
            r"\*\*?AI\s*题解摘要\*\*?[：:]\s*(.+?)(?=\n\s*\n|\n\*|$)",
            body, re.S,
        )
        if m_sum:
            summary = re.sub(r"\s+", " ", m_sum.group(1).strip())[:200]

        # 瓶颈摘要
        bottleneck = ""
        m_bot = re.search(
            r"\*\*?b\)\s*瓶颈[在][哪][里][?]?\*\*?\s*(.+?)(?=\n\s*\n|\n\*\*?c\)|$)",
            body, re.S,
        )
        if m_bot:
            bottleneck = re.sub(r"\s+", " ", m_bot.group(1).strip())[:200]

        mistakes.append({
            "idx": int(idx) if idx.isdigit() else len(mistakes) + 1,
            "problem_id": pid,
            "title": title or "(无标题)",
            "source": source,
            "summary": summary,
            "bottleneck": bottleneck,
        })

    out["mistakes"] = mistakes
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
    try:
        from docs.gesp_estimator import compute_exemptions
    except Exception:
        from gesp_estimator import compute_exemptions

    student = _admin_students.get_student_by_uid(luogu_uid)

    # v3.8 · 海报兜底：学员档案不存在时，从 reports/<uid> 找 export_data.json
    # 反查姓名 + city/school，避免老用户/匿名报告生成时海报直接失败
    if not student:
        try:
            report_dir = _find_latest_report_dir(luogu_uid, "")
            if report_dir is not None:
                export_json = report_dir / "export_data.json"
                if export_json.exists():
                    export_data = json.loads(export_json.read_text(encoding="utf-8"))
                    export_meta = (export_data.get("meta") or {})
                    student = {
                        "id": 0,  # 虚拟 ID（不参与 DB 写入）
                        "luogu_uid": luogu_uid,
                        "real_name": export_meta.get("student_name") or "学员",
                        "city": export_meta.get("city") or "",
                        "province": export_meta.get("province") or "",
                        "school": export_meta.get("school") or "",
                        "gesp_highest_passed": 0,
                        "gesp_latest_score": 0,
                        "_from_export": True,  # 标记，避免后续写 DB
                    }
                    app.logger.info(
                        f"v3.8 海报兜底: UID {luogu_uid} 学员档案不存在, 从 {export_json} 取姓名={student.get('real_name')}"
                    )
        except Exception as _e:
            app.logger.warning(f"v3.8 海报兜底读取 export_data.json 失败: {_e}")

    if not student:
        return None

    gesp_level = int(student.get("gesp_highest_passed") or 0)
    gesp_score = int(student.get("gesp_latest_score") or 0)
    # v3.9.16 · GESP 兜底：students 表 gesp_highest_passed 可能为 0（学员自录后未重算
    # 或重算逻辑遗漏），但 gesp_exams 表可能已有记录。直接查 gesp_exams 兜底。
    if not gesp_level and student.get("id"):
        try:
            from task_store import _get_conn as _get_conn_gesp
            _c = _get_conn_gesp()
            try:
                _r = _c.execute(
                    "SELECT MAX(registered_level) AS lvl, MAX(actual_score) AS sc "
                    "FROM gesp_exams WHERE student_id=? AND passed=1",
                    (int(student["id"]),),
                ).fetchone()
                if _r and _r["lvl"]:
                    gesp_level = int(_r["lvl"])
                    if int(_r["sc"] or 0) > gesp_score:
                        gesp_score = int(_r["sc"])
            finally:
                _c.close()
        except Exception:
            pass
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
    # v3.9 · 按 (date, prefix) 去重：同一天 + 同类型（如 GESP 1-4 / 5-6）的多场考试合并成一条
    # 因为 _shorten_comp_name 已经把 "1-4 级 / 5-6 级" 都映射成 "GESP 考级（夏考）"，
    # 同一组考试会出现多次（数据库是分级别存的），需要按 display name 合并
    seen: dict = {}  # key=(date, display_prefix) -> event
    for ename, edate in rows:
        try:
            d = _date.fromisoformat(edate)
            display = _shorten_comp_name(ename)
            # 提取前缀 (GESP / CSP-J / CSP-S / NOIP / NOI 等)
            prefix = display.split()[0] if display else ""
            key = (edate, prefix)
            if key in seen:
                continue  # 同一天同一类考试已记录，跳过
            seen[key] = True
            events.append({
                "name": ename,
                "display": display,
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
    """根据 GESP 等级 + 分数 + AI 定级生成 1 句 AI 评估 + 1 个标签

    返回 (评估语, 评估标签 [强/中/弱/起步])

    v3.9 · 优先参考 ai_level 字符串判定（无 GESP 成绩但有 AI 定级的不再误判"起步"）
    """
    lv = data.get("gesp_level") or 0
    sc = data.get("gesp_score") or 0
    ai_level = (data.get("ai_level") or "").strip()

    # v3.9 · 有 AI 定级字符串时，从关键词推断档位（避免 GESP 缺失导致"起步"误判）
    if ai_level:
        # 关键词优先级：强 > 中 > 起步
        strong_kw = ["提高", "S 入门", "S入门", "NOIP", "省一", "NOI", "熟练"]
        mid_kw = ["J 熟练", "J熟练", "J 入门", "J入门", "普及", "J 起步"]
        weak_kw = ["起步", "入门"]
        # 特例：含"提高"/"S"字样的定级 → 强
        if "提高" in ai_level or "S 入门" in ai_level or "S入门" in ai_level or "省一" in ai_level or "NOI" in ai_level or "NOIP" in ai_level:
            if lv and sc >= 80:
                return (f"AI 评估：高分 {sc} 分，算法基础扎实，可冲 NOI 决赛梯队", "强")
            return (f"AI 评估：AI 定级「{ai_level}」，整体能力强，建议保持节奏继续冲高分", "强")
        if "熟练" in ai_level and ("CSP-J" in ai_level or "J" in ai_level):
            if lv and sc >= 60:
                return (f"AI 评估：通过 {sc} 分，距免初赛只差 1 级，6 月可冲 8 级 60+", "中")
            return (f"AI 评估：AI 定级「{ai_level}」，处于熟练阶段，建议巩固基础冲免初赛", "中")
        if "入门" in ai_level or "普及" in ai_level:
            if lv and sc >= 60:
                return (f"AI 评估：通过 {sc} 分，建议巩固 {lv} 级 → 下一目标 {lv+1} 级 80+", "中")
            return (f"AI 评估：AI 定级「{ai_level}」，处于入门阶段，建议系统补齐算法基础", "中")

    # 无 ai_level 时的兜底（按 GESP 等级 + 分数判定）
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
    # v3.8 · qrcode + PIL 缺失时降级（不抛 500），QR 位置显示纯文字 URL
    # 优先用 pillow 后端（PilImage），失败再回退 PyPNGImage（需 pypng 库）
    _HAS_QRCODE = False
    _QR_FACTORY = None
    try:
        import qrcode  # type: ignore
        from PIL import Image as _PILImage  # type: ignore
        try:
            # 优先：pillow 后端（pillow 已装就能用）
            from qrcode.image.pil import PilImage as _QR_FACTORY  # type: ignore
            _HAS_QRCODE = True
        except Exception:
            # 回退：PyPNG 后端（需要 pypng）
            try:
                from qrcode.image.pure import PyPNGImage as _QR_FACTORY  # type: ignore
                _HAS_QRCODE = True
            except Exception as _e2:
                app.logger.warning(
                    f"v3.8 海报二维码降级（qrcode + pillow 装了但 pypng 没装: {_e2}），"
                    f"建议 pip install pypng"
                )
                _PILImage = None  # type: ignore
    except Exception as _qr_e:
        app.logger.warning(
            f"v3.8 海报二维码降级（qrcode/PIL 未安装: {_qr_e}），将仅显示 URL 文字"
        )
        _PILImage = None  # type: ignore

    # 中文字体（兼容 Windows / Linux）
    # 注意：matplotlib 渲染 emoji 会失败（普通 TrueType 字体不含 emoji 字形）
    # 本设计完全使用 ASCII 符号 + 几何形状，无需 emoji 字体
    # v3.9 · 容器内补充 WenQuanYi Micro Hei / Noto Sans CJK（修复 Linux 中文方框）
    plt.rcParams["font.sans-serif"] = [
        "Microsoft YaHei", "SimHei", "SimSun",  # Windows
        "WenQuanYi Micro Hei", "WenQuanYi Zen Hei",  # Debian/Ubuntu
        "Noto Sans CJK SC", "Noto Sans CJK JP",  # 通用 Linux (Noto)
        "Source Han Sans SC", "PingFang SC",  # macOS / Noto 别名
        "DejaVu Sans",  # 最后兜底（无中文）
    ]
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
        level_text = ai_level
        # v3.9.34 · 按文本长度动态算 fontsize，并按标点智能断行成 ≤2 行，
        # 避免 "CSP-S 入门者，尚未达到 CSP-S 合格水平" 这类长定级溢出卡片右边
        # 经验值：1 个汉字 ≈ 1.0pt 宽，字号 28pt 时一行约 12 个汉字能塞进 7.3 轴单位
        char_count = len(level_text)
        if char_count <= 12:
            level_fontsize = 28
        elif char_count <= 16:
            level_fontsize = 24
        elif char_count <= 20:
            level_fontsize = 21
        elif char_count <= 24:
            level_fontsize = 18
        elif char_count <= 30:
            level_fontsize = 16
        else:
            level_fontsize = 14
        # 超 16 字符：尝试在标点（，、；： /  ·  →）处断成 2 行
        if char_count > 16:
            break_chars = ["，", "、", "；", "：", " ", "（", ")", "(", ")", "·", "→", ">", "/"]
            target = char_count // 2
            best_split = -1
            for off in range(0, 6):
                for cand in (target + off, target - off):
                    if 4 <= cand < char_count and level_text[cand] in break_chars:
                        best_split = cand + 1
                        break
                if best_split != -1:
                    break
            if best_split == -1:
                # 没找到标点 → 直接在中间硬切
                best_split = char_count // 2
            level_text = level_text[:best_split].rstrip() + "\n" + level_text[best_split:].lstrip()
    else:
        level_text = "尚未生成报告"
        level_fontsize = 28
        # v3.9.36 · 修 v3.9.34 漏初始化：else 分支必须给 char_count 赋值，
        # 否则下面 _sub_y = ... if char_count <= 16 ... 抛 UnboundLocalError → 500
        char_count = len(level_text)

    # 大字（label/大字/小标签 三组留白均衡）
    ax.text(0.85, 11.85, level_text, ha="left", va="center",
            fontsize=level_fontsize, color=COLOR_PRIMARY_DK, fontweight="bold",
            path_effects=[pe.withStroke(linewidth=0.4, foreground=COLOR_PRIMARY_LT)])
    # 小标签（断行后下移一点，避免与大字的 2 行重叠）
    _sub_y = 11.20 if char_count <= 16 else 10.95
    if ai_level:
        ax.text(0.85, _sub_y, "（基于 NOI 2025 大纲 · AI 综合判定）",
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
    # v3.8 · qrcode 缺失时降级：QR 白底 + URL 文字仍渲染，但不显示二维码图
    qr_rendered = False
    if _HAS_QRCODE and _QR_FACTORY is not None:
        try:
            qr = qrcode.QRCode(version=2, box_size=8, border=2, image_factory=_QR_FACTORY)
            qr.add_data(qr_url)
            qr.make(fit=True)
            qr_img = qr.make_image(fill_color="black", back_color="white")
            qr_buf = io.BytesIO()
            qr_img.save(qr_buf, "PNG")
            qr_buf.seek(0)

            qr_pil = _PILImage.open(qr_buf).convert("RGBA")
            qr_target = 1.5
            # 用 ax.imshow 直接以 ax 数据坐标定位（不受 figure 边距影响）
            # 居中于 QR 白底 (5.55, 0.40, 2.85, 2.10) — 中心 (6.975, 1.45)
            qr_left = 6.975 - qr_target / 2
            qr_bottom = 1.45 - qr_target / 2
            ax.imshow(qr_pil, extent=[qr_left, qr_left + qr_target,
                                       qr_bottom, qr_bottom + qr_target],
                      aspect="equal", zorder=3, interpolation="nearest")
            qr_rendered = True
        except Exception as _qr_e:
            app.logger.warning(f"v3.8 海报二维码生成失败（不影响主流程）: {_qr_e}")

    # QR 白底（与左侧文字同高，QR 码居中）
    ax.add_patch(_rounded(5.55, 0.40, 2.85, 2.10, COLOR_CARD,
                          ec=COLOR_CARD_EDGE, lw=1, r=0.20))
    # 若二维码未生成，在白底中央显示一个 "⚠" 占位 + "扫码暂不可用" 提示
    if not qr_rendered:
        ax.text(6.975, 1.55, "⚠", ha="center", va="center",
                fontsize=32, color=COLOR_AMBER)
        ax.text(6.975, 1.00, "扫码暂不可用", ha="center", va="center",
                fontsize=10, color=COLOR_TEXT_LT)
    # 左侧文字（与 QR 白底顶部对齐；脚注 y=2.30，"扫码..." y=2.00 距脚注 0.30）
    # 用紫色方块代替 📱 emoji
    ax.add_patch(_rounded(0.50, 1.89, 0.22, 0.22, COLOR_PRIMARY, r=0.05))
    ax.text(0.61, 2.00, "Q", ha="center", va="center",
            fontsize=12, color="white", fontweight="bold")
    ax.text(0.85, 2.00, "扫码查看完整 AI 测评",
            ha="left", va="center", fontsize=13, color=COLOR_TEXT, fontweight="bold")
    ax.text(0.50, 1.60, "免费 AI 测评 · 3 分钟出报告",
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
    """v3.5.2 传播期 · 位置图 PNG（学员自助中心"生成"按钮所调）

    v3.8 · 优先读取报告生成时已预渲染的 PNG（v3.8 异步任务产物），
    缺失时再走现场 matplotlib 渲染（兜底）。

    v3.9.37 · 海报缺失时自动重新生成（兜底渲染），并把产物落盘到
    `reports/<uid>/share-card.png`，下次访问直接走缓存（不再走 5-15s 渲染）。
    适用于：(1) deploy.sh 误删 reports/；(2) 老报告未渲染海报。
    """
    # 1) 优先从最新 report 目录读取已预渲染的 PNG
    student = _admin_students.get_student_by_uid(luogu_uid)
    if student:
        report_dir = _find_latest_report_dir(luogu_uid, student.get("real_name") or "")
        if report_dir:
            cached = report_dir / "share-card.png"
            if cached.exists():
                resp = send_file(str(cached), mimetype="image/png", conditional=True)
                resp.headers["Cache-Control"] = "public, max-age=600"
                return resp
    # 2) 兜底：现场渲染（5-15s），并把结果落盘到 reports/<uid>/share-card.png
    data = _build_share_card_data(luogu_uid)
    if not data:
        return "UID 未注册", 404
    base = request.host_url.rstrip("/")
    qr_url = f"{base}/r/{luogu_uid}"  # v3.7 · 指向新建的报告预览中转页
    try:
        png_bytes = _render_share_card_png(data, qr_url)
    except Exception as _e:
        app.logger.exception(f"v3.9.37 share-card.png 兜底渲染失败: UID={luogu_uid}: {_e}")
        return f"海报生成失败: {_e}", 500
    # v3.9.37 · 落盘缓存（让下次访问走 send_file 快速返回）
    try:
        # 解析 _find_latest_report_dir 同一份报告目录（如果存在）
        if student:
            _dir = _find_latest_report_dir(luogu_uid, student.get("real_name") or "")
        else:
            _dir = _find_latest_report_dir(luogu_uid, data.get("name") or "")
        if _dir:
            _dir.mkdir(parents=True, exist_ok=True)
            (_dir / "share-card.png").write_bytes(png_bytes)
            app.logger.info(f"v3.9.37 share-card.png 兜底渲染并缓存: {_dir / 'share-card.png'} ({len(png_bytes)} bytes)")
        else:
            # 没有任何 report 目录：建一个 reports/<uid>/share-card.png 占位
            _fallback = Path(__file__).parent / "reports" / luogu_uid
            _fallback.mkdir(parents=True, exist_ok=True)
            (_fallback / "share-card.png").write_bytes(png_bytes)
            app.logger.info(f"v3.9.37 share-card.png 兜底渲染并缓存到新目录: {_fallback / 'share-card.png'} ({len(png_bytes)} bytes)")
    except Exception as _cache_e:
        # 缓存失败不影响本次返回
        app.logger.warning(f"v3.9.37 share-card.png 落盘缓存失败（不影响本次返回）: {_cache_e}")
    return Response(png_bytes, mimetype="image/png", headers={
        "Content-Disposition": f'inline; filename="share-card-{luogu_uid}.png"',
        "Cache-Control": "public, max-age=600",
    })


# ---- v3.7 · 报告预览中转页（公开，陌生人扫码落地） ----

def _extract_achievements_from_report(report_md: str) -> dict:
    """v3.7 · 从 report.md 抽成就数据，供 /r/<uid> 模板渲染。

    返回 dict：
      - six_dim: dict[str,int]   6 维能力评分（基础算法/数据结构/图论/DP/字符串/数学）
      - ai_score_thousand: int|None  AI 评测分（0-1000，None 表无）
      - ai_score_label: str          等级文字
      - mistakes: list[dict]         错题条目（idx/problem_id/title/source/summary）

    v3.9 · 兼容新报告生成器的格式：
      - 6 维表：旧 `| **基础算法** | **72** |` / 新 `| 基础算法 | 72 |` 都能匹配
      - 错题：旧 `**Pxx**` 包裹 / 新 `| Pxx [xx] 标题 | 次数 | 未 AC |` 都能匹配

    v3.9.6 · 重大修复：实际报告用 `**B2026**` 这种 **加粗** + **B/P 两种题号**前缀
      + 章节 10.1+ 用 `### 10.1 B2026 标题` 格式，老正则全 miss。
      修了：
        1. pattern A 支持 `**` 加粗标记 + `[BPUV]\\d{4,6}` 全部洛谷题号
        2. pattern C 章节标题解析支持 B/P/U/V 全部前缀
        3. **新增来源 D**：从 `export_data.json.failed_items` 兜底（最权威，数据源）
    """
    import re as _re
    out = {
        "six_dim": {},
        "ai_score_thousand": None,
        "ai_score_label": "—",
        "mistakes": [],
    }
    if not report_md:
        return out

    # 1) 6 维评分：兼容多种格式
    six_dim_keys = ["基础算法", "数据结构", "图论", "动态规划", "字符串", "数学"]
    for k in six_dim_keys:
        m = None
        # 格式 A（旧）：`| **基础算法** | **72** | ...`（key 与 score 都加粗）
        m = _re.search(rf"\*\*\s*{_re.escape(k)}\s*\*\*\s*\|\s*\*\*\s*(\d+)\s*\*\*", report_md)
        if not m:
            # 格式 B（新报告）：`| **基础算法** | 95 | ...`（仅 key 加粗，score 数字未加粗）
            m = _re.search(
                rf"\*\*\s*{_re.escape(k)}\s*\*\*\s*\|\s*\*?\s*(\d{{1,3}})\s*\|",
                report_md,
            )
        if not m:
            # 格式 C（新报告纯文本）：`| 基础算法 | 72 | ...`（无加粗）
            m = _re.search(
                rf"\|\s*{_re.escape(k)}\s*\|\s*(\d{{1,3}})\s*\|",
                report_md,
            )
        if not m:
            # 格式 D：HTML 行内 `<td><b>基础算法</b></td><td>95</td>`（SVG 报告兜底）
            m = _re.search(
                rf"<(?:b|strong)>\s*{_re.escape(k)}\s*</(?:b|strong)>\s*</td>\s*<td[^>]*>\s*(\d{{1,3}})\s*</td>",
                report_md,
                _re.IGNORECASE,
            )
        if m:
            try:
                v = int(m.group(1))
                if 0 <= v <= 100:
                    out["six_dim"][k] = v
            except Exception:
                pass

    # 2) AI 评测分：用 6 维均值 × 10 估算（avg 0-100 → 0-1000）
    if out["six_dim"]:
        avg = sum(out["six_dim"].values()) / len(out["six_dim"])
        score = int(round(avg * 10))
        out["ai_score_thousand"] = max(0, min(1000, score))
        if score >= 750:
            out["ai_score_label"] = "🟢 优秀"
        elif score >= 550:
            out["ai_score_label"] = "🟡 良好"
        elif score >= 350:
            out["ai_score_label"] = "🟠 基础"
        else:
            out["ai_score_label"] = "🔴 待提升"

    # 3) 错题本：兼容多种来源
    #    来源 A: v3.9 新报告 "死磕题目 TOP" 表（**B2026** 加粗 + 4 列：题目/次数/状态/分析）
    #            `| **B2026** 计算浮点数相除的余 | 4 | 未AC（语法错误 ×1，WA ×3） | <p...>...`
    #    来源 B: v3.7 旧格式 `| **P11229** | [CSP-J 2024] 小木棍 | ... | **24次** | 未AC | <原因>`
    #    来源 C: v3.6 旧报告 "未通过题目" 段（### 10.1 B2026 标题）
    #    来源 D: v3.9.6 新增 · export_data.json.failed_items（最权威，兜底）
    seen_pids: set = set()  # 避免重复
    pid_re = r"[BPUV]\d{4,6}"  # v3.9.6 · 洛谷题号：P=普通 / B=入门 / U=Universal / V=？

    # 来源 A（新报告主源 · 已兼容 ** 加粗 + B/P 前缀）
    pat_a = _re.compile(
        r"\|\s*\*+\s*(" + pid_re + r")\s*\*+\s*([^\|]*?)\s*\|\s*\d+\s*\|\s*"
        r"(?:未\s*AC|未AC|WA|未通过|未\s*通过)[^|]*\|\s*([^|\n]+?)(?:\s*\||\s*$)",
        _re.M,
    )
    idx = 0
    for m in pat_a.finditer(report_md):
        idx += 1
        problem_id = m.group(1).strip()
        if problem_id in seen_pids:
            continue
        seen_pids.add(problem_id)
        # 第 2 段是题目标题（可能带 [来源]）
        title_full = m.group(2).strip()
        sm = _re.match(r"\[([^\]]+)\]\s*(.+)", title_full)
        if sm:
            source, title = sm.group(1).strip(), sm.group(2).strip()
        else:
            source, title = "", title_full or problem_id
        # 第 3 段是 AI 分析（可能带 HTML 标签）
        summary = _re.sub(r"<[^>]+>", "", m.group(3) or "").strip()[:200]
        out["mistakes"].append({
            "idx": idx,
            "problem_id": problem_id,
            "title": title or problem_id,
            "source": source,
            "summary": summary,
        })

    # 来源 B（v3.7 旧报告 · 同样兼容 B/P 前缀）
    if not out["mistakes"]:
        pat_b = _re.compile(
            r"\|\s*\*\*\s*(" + pid_re + r")\s*\*\*\s*\|\s*"
            r"(?:\[[^\]]+\]\s*)?([^\|]+?)\s*\|\s*"
            r"[^\|]+?\s*\|\s*\*\*\s*\d+\s*次\s*\*\*\s*\|\s*"
            r"(?:未AC|未\s*AC|WA|未通过)\s*\|\s*([^|\n]+?)(?:\s*\||\s*$)",
            _re.M,
        )
        for m in pat_b.finditer(report_md):
            idx += 1
            problem_id = m.group(1).strip()
            if problem_id in seen_pids:
                continue
            seen_pids.add(problem_id)
            title_full = m.group(2).strip()
            sm = _re.match(r"\[([^\]]+)\]\s*(.+)", title_full)
            if sm:
                source, title = sm.group(1).strip(), sm.group(2).strip()
            else:
                source, title = "", title_full
            summary = m.group(3).strip()
            out["mistakes"].append({
                "idx": idx,
                "problem_id": problem_id,
                "title": title,
                "source": source,
                "summary": summary,
            })

    # 来源 C（v3.6 旧报告：未通过题目章节 · 兼容 B/P 前缀）
    if not out["mistakes"]:
        anchor = _re.search(r"^#{2,4}\s*10[.、]?\s*[【\[]?未通过题目.*?$", report_md, _re.M)
        if anchor:
            section = report_md[anchor.end():]
            # 找所有 ### 10.N 标题 块
            blocks = _re.split(r"^#{3,4}\s*(\d+)\.\s*(.+?)$", section, flags=_re.M)
            i = 1
            while i + 2 < len(blocks):
                title_line = blocks[i + 1].strip()
                body = blocks[i + 2]
                i += 3
                # v3.9.6 · 支持 P/B/U/V 题号 + 可选 [来源]
                mp = _re.match(r"(" + pid_re + r")\s*(?:\[([^\]]+)\])?\s*(.+)", title_line)
                if not mp:
                    continue
                pid = mp.group(1)
                if pid in seen_pids:
                    continue
                seen_pids.add(pid)
                source = (mp.group(2) or "").strip()
                title = (mp.group(3) or "").strip() or pid
                # 抓 AI 题解摘要
                ms = _re.search(
                    r"\*\*?AI\s*题解摘要\*\*?[：:]\s*(.+?)(?=\n\s*\n|\n\*|$)",
                    body, _re.S,
                )
                summary = _re.sub(r"\s+", " ", ms.group(1).strip())[:200] if ms else ""
                idx += 1
                out["mistakes"].append({
                    "idx": idx,
                    "problem_id": pid,
                    "title": title,
                    "source": source,
                    "summary": summary,
                })

    return out


def _sanitize_ref(raw: str | None) -> str:
    """v3.7 · 规范化 ref 参数：仅保留 [A-Za-z0-9_-]，≤32 字符。"""
    if not raw:
        return ""
    import re as _re
    s = _re.sub(r"[^A-Za-z0-9_-]", "_", str(raw).strip())
    return s[:32]


def _resolve_gesp_level_score(student: dict) -> tuple:
    """v3.9.29 · GESP 段位+分数 3 层兜底（学生表 → gesp_exams 表）

    返回 (level, score) 元组。任意一层有值就返回。
    之前 /r/<uid> 路由只读 student.gesp_highest_passed（学生表），遇到
    gesp_exams 表有记录但 students 表没更新（用户自录后未重算）的情况就显示 0。
    现在跟 /me 路由一样，先读 students 表，0 则查 gesp_exams 表。

    错误防御：gesp_exams 表可能不存在、字段可能不同，全部 try/except。
    """
    if not student:
        return (0, 0)
    _gh = 0
    _gs = 0
    try:
        _gh = int(student.get("gesp_highest_passed") or 0)
        _gs = int(student.get("gesp_latest_score") or 0)
    except Exception:
        pass
    if _gh and _gh > 0:
        return (_gh, _gs)
    # 第二层：gesp_exams 表兜底
    try:
        sid = int(student.get("id") or 0)
        if sid > 0:
            from task_store import _get_conn as _gconn
            _gc = _gconn()
            try:
                _gr = _gc.execute(
                    "SELECT MAX(registered_level) AS lvl, MAX(actual_score) AS sc "
                    "FROM gesp_exams WHERE student_id=? AND passed=1",
                    (sid,),
                ).fetchone()
                if _gr and _gr["lvl"]:
                    return (int(_gr["lvl"]), int(_gr["sc"] or 0))
            finally:
                _gc.close()
    except Exception:
        pass
    return (_gh, _gs)


@app.route("/r/<luogu_uid>", methods=["GET"])
def report_preview(luogu_uid: str):
    """v3.7 · 报告预览中转页（公开，陌生人扫码落地）

    v3.9 · 把真实报告目录名（reports/<dir_name>）传给模板，修复"扫码不跳转" bug
    """
    raw_ref = request.args.get("ref")
    ref = _sanitize_ref(raw_ref)

    # v3.9 · 修复"扫码不跳转" bug：用学员表的姓名查最新报告目录
    # （旧逻辑只传 luogu_uid，找不到没有 luogu_uid.txt 侧车文件的报告目录）
    student = _admin_students.get_student_by_uid(luogu_uid)
    student_name = (student.get("real_name") or "").strip() if student else ""
    latest = _find_latest_report_dir(luogu_uid, student_name)
    empty_achievements = {
        "six_dim": {},
        "ai_score_thousand": None,
        "ai_score_label": "—",
        "mistakes": [],
    }

    # v3.9 · 实际报告目录名（用于"看完整 AI 报告"按钮的 URL）
    # latest 是 Path 对象，目录名形如 "d794a8b0_付胤睿" / "25c937b3_付胤睿"
    latest_dir_name = latest.name if latest else ""

    if not latest or not (latest / "report.md").exists():
        # v3.9.29 · 即使没 report.md，也走 export_data 兜底（之前直接 has_report=False）
        _ext_fb = {}
        try:
            if latest and (latest / "export_data.json").exists():
                _ext_fb = _extract_achievements_from_export_data(latest) or {}
                if _ext_fb.get("six_dim"):
                    _ext_fb["six_dim_source"] = "export_data"
                if _ext_fb.get("ai_score_thousand"):
                    _ext_fb["ai_score_source"] = "export_data"
                    _ext_fb["ai_score_label"] = f"预估 {_ext_fb['ai_score_thousand']}/1000（AI 报告未生成，6 维+评分来自 export_data.json）"
                if not (latest / "report.md").exists():
                    _ext_fb["is_partial"] = True
                _ext_fb["report_dir"] = latest.name if latest else ""
        except Exception:
            pass
        # v3.9.29 · 3 层 GESP 兜底（student 表 → gesp_exams 表）
        _gh, _gs = _resolve_gesp_level_score(student)
        return render_template_string(
            REPORT_PREVIEW_HTML,
            luogu_uid=luogu_uid,
            token=luogu_uid,  # v3.9.29 · 模板 header 用 {{ token }} 渲染 UID
            student_name=(student.get("real_name") or f"UID {luogu_uid}"),
            achievements=_ext_fb or empty_achievements,
            ai_summary="",
            suggestions=[],
            ref=ref,
            has_report=bool(_ext_fb.get("six_dim") or _ext_fb.get("mistakes")),
            latest_dir_name=latest_dir_name,
            gesp_level=_gh,
            gesp_score=_gs,
        ), 200

    try:
        report_md = (latest / "report.md").read_text(encoding="utf-8", errors="replace")
        achievements = _extract_achievements_from_report(report_md) or empty_achievements
        ai_summary = _extract_ai_summary(report_md) or ""
        suggestions = _extract_top_suggestions(report_md) or []

        # v3.9.29 · 3 级兜底：之前只读 report.md，没匹配到 6 维/AI 评分就一直空。
        # 跟 /me 路由一致：逐字段补全（report.md 已读到的优先，否则 export_data.json 兜底）。
        try:
            if (latest / "export_data.json").exists():
                _ext_fb = _extract_achievements_from_export_data(latest) or {}
                if not achievements.get("six_dim") and _ext_fb.get("six_dim"):
                    achievements["six_dim"] = _ext_fb["six_dim"]
                    achievements["six_dim_source"] = "export_data"
                if not achievements.get("mistakes") and _ext_fb.get("mistakes"):
                    achievements["mistakes"] = _ext_fb["mistakes"]
                if not achievements.get("ai_score_thousand") and _ext_fb.get("ai_score_thousand"):
                    achievements["ai_score_thousand"] = _ext_fb["ai_score_thousand"]
                    achievements["ai_score_source"] = "export_data"
                    if achievements.get("six_dim_source") == "report_md":
                        _mean = sum(achievements["six_dim"].values()) / max(1, len(achievements["six_dim"]))
                        achievements["ai_score_label"] = f"预估 {int(round(_mean * 10))}/1000（AI 报告 6 维已抽取；评分由 6 维均值 × 10 兜底）"
                    else:
                        achievements["ai_score_label"] = f"预估 {_ext_fb['ai_score_thousand']}/1000（AI 报告 6 维 regex 未匹配，评分来自 export_data.json）"
                if (not achievements.get("six_dim") and not achievements.get("mistakes")):
                    achievements["is_partial"] = True
                if achievements.get("six_dim") and not achievements.get("six_dim_source"):
                    achievements["six_dim_source"] = "report_md"
                if achievements.get("ai_score_thousand") and not achievements.get("ai_score_source"):
                    achievements["ai_score_source"] = "report_md"
        except Exception as _de:
            app.logger.warning(f"[v3.9.29 /r/{luogu_uid}] 3 级兜底失败: {_de}")

        # v3.9.6 · 来源 D 兜底：report.md 正则没抓到错题时，从 export_data.json.failed_items 拿
        # 这是数据源本身，最权威。report.md 是 AI 生成的衍生品，可能格式漂移。
        if not achievements.get("mistakes"):
            try:
                import json as _json
                _exp_path = latest / "export_data.json"
                if _exp_path.exists():
                    _exp = _json.loads(_exp_path.read_text(encoding="utf-8", errors="replace"))
                    _fi = _exp.get("failed_items") or []
                    if _fi:
                        _mistakes = []
                        for k, fi in enumerate(_fi, start=1):
                            p = fi.get("problem") or {}
                            if not isinstance(p, dict):
                                continue
                            _pid = (p.get("pid") or "").strip()
                            if not _pid:
                                continue
                            _title = (p.get("title") or "未命名题目").strip()
                            _tag_ids = p.get("tags") or []
                            _tag = str(_tag_ids[0]) if (_tag_ids and isinstance(_tag_ids[0], str)) else ""
                            _mistakes.append({
                                "idx": k,
                                "problem_id": _pid,
                                "title": _title,
                                "source": _tag,
                                "summary": "",
                            })
                        if _mistakes:
                            achievements = dict(achievements)
                            achievements["mistakes"] = _mistakes
                            app.logger.info(
                                f"[v3.9.6 /r/{luogu_uid}] 来源 D 兜底：export_data.json → {len(_mistakes)} 道错题"
                            )
            except Exception as _de:
                app.logger.warning(f"[v3.9.6 /r/{luogu_uid}] 来源 D 兜底失败: {_de}")
    except Exception:
        _gh2, _gs2 = _resolve_gesp_level_score(student)
        return render_template_string(
            REPORT_PREVIEW_HTML,
            luogu_uid=luogu_uid,
            token=luogu_uid,
            student_name=(student.get("real_name") or f"UID {luogu_uid}"),
            achievements=empty_achievements,
            ai_summary="",
            suggestions=[],
            ref=ref,
            has_report=False,
            latest_dir_name=latest_dir_name,
            gesp_level=_gh2,
            gesp_score=_gs2,
        ), 200

    _gh3, _gs3 = _resolve_gesp_level_score(student)
    return render_template_string(
        REPORT_PREVIEW_HTML,
        luogu_uid=luogu_uid,
        token=luogu_uid,
        student_name=(student.get("real_name") or f"UID {luogu_uid}"),
        achievements=achievements,
        ai_summary=ai_summary,
        suggestions=suggestions,
        ref=ref,
        has_report=True,
        latest_dir_name=latest_dir_name,
        gesp_level=_gh3,
        gesp_score=_gs3,
    ), 200


# ---- v3.5.2 · 家长订阅版（5 维度深度分析） ----

def _build_parent_subscribe_data(student: dict, luogu_uid: str) -> dict:
    """组装家长订阅版所需的全部数据：5 维度"""
    import json as _json
    from datetime import date as _date
    try:
        from docs.gesp_estimator import compute_exemptions, next_eligible_gesp_level
    except Exception:
        from gesp_estimator import compute_exemptions, next_eligible_gesp_level

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
    """v3.9 · 家长订阅版（AI 决策支持）

    GET 行为：
      - 学员已存在 + 已有 parent_subscribe.html → 渲染 AI 生成版
      - 学员已存在 + 还没生成 → 渲染"触发生成"页（POST 触发）
      - 学员未注册 → 404

    POST 行为：直接重定向到 /me/<uid>/start-parent-subscribe（用 form 提交也行）

    v3.9 修复：去掉 _HIDE_COMMERCE 拦截。家长通过海报/链接点进来就该看到
    邀请码表单，而不是"传播期模式"占位页（之前占位页是开发者视角的，
    家长看到一头雾水）。_HIDE_COMMERCE 仍然控制模板内部商业化显示
    （如冲刺营定价），但不再让整个页面被 503 替换。
    """
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
        # v3.9 · 解析生成时间（取文件 mtime 而非 md 文件最后 200 字符）
        from datetime import datetime as _dt
        try:
            mtime = _dt.fromtimestamp(ps_md.stat().st_mtime).strftime("%Y-%m-%d %H:%M") if ps_md and ps_md.exists() else "—"
        except Exception:
            mtime = "—"
        # v3.9.30 · 家长报告静态 HTML 后处理：把 AI 报告里「您所在城市」「您城市」
        # 这种占位文替换成学生档案里的真实城市 + 省份（深圳 / 广东）。
        # 之前 AI 不知道准确城市（即使 _build_parent_subscribe_data 传了，
        # 生成的 markdown 仍说「您所在城市」），现在在渲染时按学员档案改写。
        _city = (student.get("city") or "").strip()
        _province = (student.get("province") or "").strip()
        if _city or _province:
            _full = (_city or _province)
            if _city and _province:
                _full = f"{_city} / {_province}"
            _city_replacements = [
                ("您所在城市或目标初中的招生网站", f"{_full}或目标初中的招生网站"),
                ("您所在城市或目标初中", f"{_full}或目标初中"),
                ("您所在城市的", f"{_full}的"),
                ("您所在城市", _full),
                ("您城市的具体", f"{_full}的具体"),
                ("您城市的", f"{_full}的"),
                ("您城市", _full),
            ]
            for _old, _new in _city_replacements:
                html_body = html_body.replace(_old, _new)
        return render_template_string(
            PARENT_SUBSCRIBE_RESULT_HTML,
            student_name=student.get("real_name") or "您家孩子",
            luogu_uid=luogu_uid,
            md_url=f"/reports/{report_dir.name}/parent_subscribe.md" if ps_md and ps_md.exists() else "",
            generated_at=mtime,
            report_dir_name=report_dir.name,
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
        commerce_hidden=_HIDE_COMMERCE,  # v3.9 · 传播期隐藏价格/付费字眼
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

    v3.8 · 家长订阅版的"生成"动作不再被 _HIDE_COMMERCE 总开关拦截。
    _HIDE_COMMERCE 只控制商业化页面的展示/订阅入口（AI 讲题/冲刺营等），
    但 "已经为某位用户生成家长版" 是技术功能，不应被此开关拒绝。

    v3.9 · 邀请码门控：用户必须先填写正确的 invite_code 才能触发。
          白名单来源：环境变量 PARENT_INVITE_CODES（逗号分隔）
                     兜底默认：["PARENT-SUB-DEMO-2026"]（方便演示/测试）
    """
    # v3.8 · 此处不再拦截 _HIDE_COMMERCE。生成动作属于"已购用户的技术服务"，不归商业化展示开关管。
    student = _admin_students.get_student_by_uid(luogu_uid)
    if not student:
        return render_template_string(REGISTER_INVALID_HTML, message=f"UID {luogu_uid} 未注册"), 404

    # v3.9 · 邀请码校验（在 404 之后、找 report 之前）
    # v3.9.2 · 改为查数据库 admin.activation_codes 表（sku='parent_invite'），
    #         admin 可在 /admin/codes 后台生成/管理；不再用环境变量
    # v3.9.8 · 用户反馈：邀请码验证后必须失效（一次性使用）
    #         - 拒绝已 redeemed 的码
    #         - 验证通过后立即写 redeemed_at + student_id（与 /redeem 流程一致）
    form = request.form.to_dict() if request.method == "POST" else {}
    submitted_code = (form.get("invite_code") or "").strip()
    invite_ok = False
    already_used = False
    inviter_code_id = None
    if submitted_code:
        try:
            from task_store import _get_conn as _invite_conn
            _ic = _invite_conn()
            try:
                row = _ic.execute(
                    "SELECT id, redeemed_at, student_id FROM activation_codes "
                    "WHERE code = ? AND sku = 'parent_invite' LIMIT 1",
                    (submitted_code,),
                ).fetchone()
            finally:
                _ic.close()
            if row is not None:
                row_d = dict(row)
                inviter_code_id = row_d.get("id")
                if row_d.get("redeemed_at") is not None:
                    # 已被使用过 → 拒绝（防止"一个码重复用"）
                    already_used = True
                else:
                    invite_ok = True
        except Exception as _ie:
            invite_ok = False
    if not invite_ok:
        gesp_level = int(student.get("gesp_highest_passed") or 0)
        gesp_score = int(student.get("gesp_latest_score") or 0)
        if already_used:
            error_msg = (
                f"❌ 邀请码 {submitted_code} 已被使用（每个邀请码仅可使用一次）。"
                " 请联系客服重新派发新码。"
            )
        else:
            error_msg = (
                "❌ 邀请码无效或为空。请扫码添加微信（微信号见页面右下角二维码），"
                "备注\"家长订阅\"，客服会从 /admin/codes 后台派发邀请码给您。"
            )
        return render_template_string(
            PARENT_SUBSCRIBE_HTML,
            student=student,
            luogu_uid=luogu_uid,
            has_report=True,
            report_dir_name="",
            error_msg=error_msg,
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
        ), 403

    # v3.9.8 · 一次性使用：成功验证后立即原子标记 redeemed_at + student_id
    # 用事务 + UPDATE WHERE redeemed_at IS NULL 防并发
    try:
        from task_store import _get_conn as _mark_conn
        _mc = _mark_conn()
        try:
            cur = _mc.execute(
                "UPDATE activation_codes "
                "SET redeemed_at = datetime('now'), student_id = ? "
                "WHERE id = ? AND redeemed_at IS NULL",
                (int(student["id"]), inviter_code_id),
            )
            _mc.commit()
            if cur.rowcount == 0:
                # 极小概率并发场景：被别人抢先 redeem
                already_used = True
                invite_ok = False
        finally:
            _mc.close()
        if not invite_ok:
            return render_template_string(
                PARENT_SUBSCRIBE_HTML,
                student=student,
                luogu_uid=luogu_uid,
                has_report=True,
                report_dir_name="",
                error_msg=(
                    f"❌ 邀请码 {submitted_code} 刚被其他用户使用，请联系客服重新派发。"
                ),
                gesp_level=int(student.get("gesp_highest_passed") or 0),
                gesp_score=int(student.get("gesp_latest_score") or 0),
                next_level=(int(student.get("gesp_highest_passed") or 0) + 1) if int(student.get("gesp_highest_passed") or 0) < 8 else 8,
                can_exempt_cspj=int(student.get("gesp_highest_passed") or 0) >= 7 and int(student.get("gesp_latest_score") or 0) >= 80,
                can_exempt_csps=int(student.get("gesp_highest_passed") or 0) >= 8 and int(student.get("gesp_latest_score") or 0) >= 80,
                gesp_gap=max(0, 60 - int(student.get("gesp_latest_score") or 0)) if int(student.get("gesp_highest_passed") or 0) else 60,
                target="—",
                timeline={"conservative": "—", "aggressive": "—", "fallback": "—"},
                policy_events=[],
                diff_dist={},
                last_exam=None,
                questions=[],
            ), 403
    except Exception as _me:
        # 标记失败但 invite_ok=True，仍允许进入生成流程（不阻塞用户体验）
        # 但记录错误供 admin 排查
        try:
            import traceback as _tb
            print(f"[parent_subscribe] 邀请码标记失败：{_me}\n{_tb.format_exc()}")
        except Exception:
            pass

    # 记录使用日志（v3.9.2 · 邀请码使用日志用于审计）
    try:
        from task_store import _get_conn as _log_conn
        _lc = _log_conn()
        _lc.execute(
            """CREATE TABLE IF NOT EXISTS invite_code_usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                luogu_uid TEXT NOT NULL,
                used_at TEXT NOT NULL
            )"""
        )
        _lc.execute(
            "INSERT INTO invite_code_usage_log (code, luogu_uid, used_at) VALUES (?, ?, ?)",
            (submitted_code, luogu_uid, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        _lc.commit()
        _lc.close()
    except Exception:
        pass  # 日志失败不影响主流程

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
        insert_task(task_id, status="running", message="正在生成家长订阅版...", luogu_uid=luogu_uid)
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

    v3.9.3 · 启动时自动迁移：把 task_id 关联的 luogu_uid 写到所有 reports 子目录的
              luogu_uid.txt 侧车（让"侧车文件精确匹配"分支生效，避免依赖目录名）。
    """
    reports_root = Path(__file__).parent / "reports"
    if not reports_root.exists():
        return None

    # v3.9.3 启动迁移
    try:
        from task_store import _get_conn as _tconn
        _conn = _tconn()
        try:
            # v3.9.4 · 同时支持「retry_form_json 里有 uid」+ 「luogu_uid 列已写入」两种来源
            # 旧任务（2026-06 之前）luogu_uid 列常为空，但 retry_form_json 里 form.uid 一定有
            _mapping: dict = {}
            for r in _conn.execute(
                "SELECT task_id, luogu_uid, retry_form_json FROM tasks WHERE retry_form_json IS NOT NULL AND retry_form_json != ''"
            ):
                _uid = str(r["luogu_uid"] or "").strip()
                if not _uid:
                    try:
                        import json as _json
                        _form = _json.loads(r["retry_form_json"] or "{}")
                    except Exception:
                        _form = {}
                    _uid = str(_form.get("luogu_uid") or _form.get("uid") or "").strip()
                if _uid and r["task_id"][:8]:
                    _mapping[r["task_id"][:8]] = _uid
        finally:
            _conn.close()
        for d in reports_root.iterdir():
            if not d.is_dir():
                continue
            sidecar = d / "luogu_uid.txt"
            if sidecar.exists():
                continue
            # 用目录名前 8 字符匹配 task_id
            prefix = d.name.split("_", 1)[0]
            if len(prefix) == 8 and prefix in _mapping:
                sidecar.write_text(_mapping[prefix], encoding="utf-8")
    except Exception:
        pass

    safe_name = "".join(c for c in (student_name or "") if c.isalnum() or c in "_-").strip()
    target_uid = str(luogu_uid or "").strip()

    exact: list = []
    legacy: list = []
    by_name: list = []
    for d in reports_root.iterdir():
        if not d.is_dir():
            continue
        # v3.9.18 · 放宽：report.md 或 export_data.json 任一存在即可
        # data_only 目录（只有 export_data.json、无 report.md）也能命中，
        # 让 /me 页的 AI 评测分兜底分支生效。
        has_report_md = (d / "report.md").exists()
        has_export_data = (d / "export_data.json").exists()
        if not (has_report_md or has_export_data):
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


def _list_reports_for_uid(luogu_uid: str) -> list:
    """扫描 reports/ 找该 UID 全部报告目录（按 mtime 倒序）。

    复用 _find_latest_report_dir 的三段式匹配：
      1) 侧车文件 luogu_uid.txt 精确匹配
      2) 目录名包含 luogu_uid（旧式命名）
      3) 同姓名（多 UID 兜底，按目录名 _ 切割取尾段）

    返回 [{task_id, name, mtime, mtime_str, files:[{label, kind, url, exists}], is_latest}, ...]
    """
    from datetime import datetime as _dt
    reports_root = Path(__file__).parent / "reports"
    if not reports_root.exists():
        return []
    target_uid = str(luogu_uid or "").strip()
    if not target_uid:
        return []

    matches = []
    for d in reports_root.iterdir():
        if not d.is_dir():
            continue
        # 必须有 report.md 才算"完整报告"
        if not (d / "report.md").exists():
            continue
        hit = False
        # 1) 侧车文件精确匹配
        sidecar = d / "luogu_uid.txt"
        if sidecar.exists():
            try:
                if sidecar.read_text(encoding="utf-8", errors="replace").strip() == target_uid:
                    hit = True
            except Exception:
                pass
        # 2) 旧式：目录名包含 UID
        if not hit and target_uid in d.name:
            hit = True
        if not hit:
            continue
        # 解析目录名 <task_id>_<name>
        parts = d.name.split("_", 1)
        task_id = parts[0] if parts else d.name
        name = parts[1] if len(parts) > 1 else ""
        try:
            mt = d.stat().st_mtime
            mtime_str = _dt.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            mt = 0
            mtime_str = "未知时间"
        # 候选文件（按展示顺序）
        candidates = [
            ("完整版 · report.html", "html", "report.html"),
            ("完整版 · report.pdf",  "pdf",  "report.pdf"),
            ("选手版 · me.html",     "html", "me.html"),
            ("选手版 · me.pdf",      "pdf",  "me.pdf"),
            ("家长版 · parent.html", "html", "parent.html"),
            ("家长版 · parent.pdf",  "pdf",  "parent.pdf"),
            ("教练版 · coach.html",  "html", "coach.html"),
            ("教练版 · coach.pdf",   "pdf",  "coach.pdf"),
            ("Markdown 原文",        "md",   "report.md"),
        ]
        files = []
        for label, kind, fname in candidates:
            p = d / fname
            files.append({
                "label": label,
                "kind": kind,
                "url": f"/reports/{d.name}/{fname}",
                "exists": p.exists(),
            })
        matches.append({
            "task_id": task_id,
            "name": name or "(未命名选手)",
            "dir_name": d.name,
            "mtime": mt,
            "mtime_str": mtime_str,
            "files": files,
            "file_count": sum(1 for f in files if f["exists"]),
        })

    matches.sort(key=lambda x: x["mtime"], reverse=True)
    if matches:
        matches[0]["is_latest"] = True
    return matches


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
            luogu_uid=luogu_uid,  # v3.8 · 拉取学籍 + GESP/CSP 奖项 + 当地政策匹配
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
    {{ app_skin_head() }}
</head>
<body class="app-body min-h-screen flex items-center justify-center p-4">
    <div class="app-card max-w-md w-full">
        <div class="text-center mb-4">
            <div class="app-pill app-pill-done mb-2">v3.5.2</div>
            <h1 class="app-title">学员注册</h1>
            <p class="app-subtitle">学而思图 1 模式 · 4 字段极简</p>
        </div>

        {% if error %}
        <div class="app-box app-box-red mb-4">⚠️ {{ error }}</div>
        {% endif %}

        <form method="POST" class="space-y-3">
            <div>
                <label class="app-label"><span class="text-red-500">*</span> 城市</label>
                <select name="city" required class="app-input">
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
                <label class="app-label"><span class="text-red-500">*</span> 姓名</label>
                <input type="text" name="real_name" required maxlength="20"
                       value="{{ form.real_name or '' }}"
                       placeholder="请输入姓名"
                       class="app-input">
            </div>

            <div>
                <label class="app-label"><span class="text-red-500">*</span> 年级</label>
                <select name="grade" required class="app-input">
                    <option value="">请选择年级</option>
                    {% for g_val, g_label in grades %}
                    <option value="{{ g_val }}" {% if form.grade == g_val %}selected{% endif %}>{{ g_label }}</option>
                    {% endfor %}
                </select>
            </div>

            <div>
                <label class="app-label"><span class="text-red-500">*</span> 性别</label>
                <div class="grid grid-cols-2 gap-2">
                    <label class="flex items-center justify-center gap-2 border rounded-lg px-3 py-2 cursor-pointer hover:bg-emerald-50 {% if form.gender == 'M' %}bg-emerald-50 border-emerald-500{% endif %}">
                        <input type="radio" name="gender" value="M" {% if form.gender == 'M' %}checked{% endif %} class="text-emerald-600">
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
                    <label class="app-label"><span class="text-red-500">*</span> 洛谷 UID</label>
                    <input type="text" name="luogu_uid" required pattern="[0-9]{6,10}"
                           value="{{ form.luogu_uid or '' }}"
                           placeholder="6-10 位数字"
                           class="app-input">
                    <p class="text-xs text-gray-400 mt-1">v3.5.2 借力洛谷主站实名 · 学员档案主键</p>
                </div>

                <div class="grid grid-cols-2 gap-2 mt-2">
                    <div>
                        <label class="block text-xs text-gray-600 mb-1">微信扫码（可选）</label>
                        <button type="button" onclick="document.getElementById('wechat_openid').value='demo_wx_openid_' + Math.random().toString(36).slice(2,10); this.textContent='✓ 已扫码';" class="w-full bg-emerald-500 text-white text-xs px-2 py-2 rounded-lg hover:bg-emerald-600">
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
                               class="app-input">
                    </div>
                </div>

                <div class="mt-2">
                    <label class="block text-xs text-gray-600 mb-1">出生日期（可选 · 用于 CSP 年龄判定）</label>
                    <input type="date" name="birth_date"
                           value="{{ form.birth_date or '' }}"
                           class="app-input">
                </div>
            </div>

            <div class="flex items-start gap-2 pt-2">
                <input type="checkbox" name="agree" id="agree" required
                       {% if form.agree %}checked{% endif %}
                       class="mt-1">
                <label for="agree" class="text-xs text-gray-600">
                    已阅读并同意 <a href="#" class="app-link">《用户协议》</a>
                    和 <a href="#" class="app-link">《未成年人个人信息保护知情同意书》</a>
                    （PIPL §5.2 · 14 岁以下需监护人陪同）
                </label>
            </div>

            <button type="submit" class="app-btn app-btn-primary">
                ✅ 完成注册
            </button>
        </form>

        <div class="text-center mt-4">
            <a href="/me/999105" class="text-xs text-gray-400 hover:text-gray-600">→ 体验已注册学员 /me/999105</a>
        </div>
        <div class="text-center mt-2">
            <a href="/" class="text-xs text-emerald-700 hover:underline">← 返回首页</a>
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
    {{ app_skin_head() }}
    <style>
        /* v3.7.1 · 个人中心：保留特色 hero header，但加入首页 app-body 渐变 + app-card 卡片 */
        .me-hero{background:linear-gradient(135deg,#059669 0%,#0d9488 100%);color:#fff;border-radius:16px;box-shadow:0 10px 25px rgba(0,0,0,.06);padding:24px;}
    </style>
</head>
<body class="app-body p-4">
    <div class="me-hero max-w-3xl mx-auto mb-4">
        <h1 class="text-2xl font-extrabold mb-1">🎓 学员 Pro · v3.5.2</h1>
        <p class="text-sm opacity-90">欢迎，<strong>{{ student.real_name or ('UID-' + student.luogu_uid) }}</strong></p>
        <p class="text-xs opacity-75 mt-1">
            UID {{ student.luogu_uid }}
            · {{ student.province or '' }} {{ student.city or '城市未填' }}
            · {% if student.gender == 'M' %}男生{% elif student.gender == 'F' %}女生{% else %}性别未填{% endif %}
            · 年级 {{ student.grade_label or student.grade or '—' }}
            · 注册渠道 {{ student.registered_via or 'admin' }}
        </p>
    </div>

    <div class="max-w-3xl mx-auto p-4 -mt-4">
        {# v3.9.7 · 顶部 GESP 段位进度条已删除（下面"个人成就"卡片里已有 GESP 段位 + 免初赛状态） #}
        {# v3.9.7 · 历史奖项录入模块已删除（注册表单里已经收集过，避免重复入口） #}

        <!-- v3.6 · 个人成就（千分制 AI 评分 + 6 维雷达 + GESP/奖项汇总） -->
        <div class="bg-white rounded-2xl shadow p-5 mb-4" id="achievements">
            <h2 class="text-lg font-bold text-gray-800 mb-3">🏅 我的个人成就</h2>

            <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
                {# v3.9.31 · 标题从「AI 评测分」改为「能力总分（6 维均分）」——之前误导
                    用户以为是 AI 给的分数，实际是 6 维均分 × 10 算的派生值。
                    6 维本身来自学员练习数据（export_data.json），不是 AI 评的。 #}
                <div class="bg-gradient-to-br from-amber-50 to-yellow-50 border border-amber-200 rounded-xl p-4 text-center">
                    <div class="text-xs text-amber-700 font-bold" title="数据来源: {{ achievements.report_dir or '暂无' }}">📊 能力总分（6 维均分 × 10）</div>
                    {% if achievements.ai_score_thousand is not none %}
                    <div class="text-4xl font-extrabold text-amber-700 mt-1">{{ achievements.ai_score_thousand }}</div>
                    <div class="text-xs text-amber-600 mt-1 px-1 break-words leading-snug">{{ achievements.ai_score_label }} · 满分 1000</div>
                    {# v3.9.31 · label 区分数据源（之前「AI 报告未生成」红色警示容易让人以为 723 是瞎编的） #}
                    {% if achievements.six_dim_source == 'export_data' %}
                    <div class="text-[10px] text-sky-700 mt-1.5 bg-sky-50 rounded px-1.5 py-0.5">
                        ℹ️ 6 维数据来自 export_data.json（练习阶段真实记录）
                    </div>
                    {% elif achievements.is_partial %}
                    <div class="text-[10px] text-amber-700 mt-1.5 bg-amber-50 rounded px-1.5 py-0.5">
                        💡 AI 报告 6 维已抽取，分数由 6 维均分 × 10 兜底
                    </div>
                    {% endif %}
                    {% else %}
                    <div class="text-3xl font-extrabold text-gray-300 mt-1">—</div>
                    <div class="text-xs text-gray-400 mt-1">暂无练习数据</div>
                    {% endif %}
                </div>

                <!-- GESP 段位 -->
                <div class="bg-gradient-to-br from-green-50 to-emerald-50 border border-green-200 rounded-xl p-4 text-center">
                    <div class="text-xs text-green-700 font-bold">🏆 GESP 段位</div>
                    {# v3.9.18 · GESP 段位优先读 student_dict.gesp_highest_passed（视图层已做三层兜底：students → gesp_exams） #}
                    {% set gesp_display_level = student.gesp_highest_passed or 0 %}
                    {% set gesp_display_score = student.gesp_latest_score or 0 %}
                    {% if gesp_display_level %}
                    <div class="text-4xl font-extrabold text-green-700 mt-1">{{ gesp_display_level }}<span class="text-lg"> 级</span></div>
                    <div class="text-xs text-green-600 mt-1">
                        {% if gesp_display_score %}真考 {{ gesp_display_score }} 分{% else %}免初赛通道{% endif %}
                    </div>
                    {% else %}
                    <div class="text-3xl font-extrabold text-gray-300 mt-1">—</div>
                    <div class="text-xs text-gray-400 mt-1">尚未录入 GESP 真考</div>
                    {% endif %}
                </div>

                <!-- 信息学竞赛奖项数 -->
                <div class="bg-gradient-to-br from-blue-50 to-indigo-50 border border-blue-200 rounded-xl p-4 text-center">
                    <div class="text-xs text-blue-700 font-bold">🏅 信息学奖项</div>
                    <div class="text-4xl font-extrabold text-blue-700 mt-1">{{ award_summary.total_awards or 0 }}</div>
                    <div class="text-xs text-blue-600 mt-1">条已录入 · CSP/NOIP/NOI</div>
                </div>
            </div>

            <!-- 6 维能力雷达（迷你条形版） -->
            {% if achievements.six_dim %}
            <div class="mt-4 border-t border-gray-100 pt-3">
                {# v3.9.28 · 「来自最新 AI 报告」改为动态（与下面数据来源一致） #}
                {% if achievements.six_dim_source == 'export_data' %}
                <div class="text-xs text-gray-500 mb-2">📊 6 维能力评分（来自 export_data.json）</div>
                {% else %}
                <div class="text-xs text-gray-500 mb-2">📊 6 维能力评分（来自最新 AI 报告）</div>
                {% endif %}
                <div class="space-y-1.5">
                    {% for k, v in achievements.six_dim.items() %}
                    <div class="flex items-center gap-2 text-xs">
                        <div class="w-20 text-gray-600 text-right">{{ k }}</div>
                        <div class="flex-1 bg-gray-100 rounded-full h-2.5 overflow-hidden">
                            <div class="h-full rounded-full
                                {% if v >= 75 %}bg-green-500
                                {% elif v >= 55 %}bg-emerald-400
                                {% elif v >= 40 %}bg-amber-400
                                {% else %}bg-red-400{% endif %}" style="width: {{ v }}%"></div>
                        </div>
                        <div class="w-10 text-right font-mono font-bold
                            {% if v >= 75 %}text-green-700
                            {% elif v >= 55 %}text-emerald-700
                            {% elif v >= 40 %}text-amber-700
                            {% else %}text-red-700{% endif %}">{{ v }}</div>
                    </div>
                    {% endfor %}
                </div>
                {% if achievements.report_dir %}
                {# v3.9.28 · 「数据来源」改为动态显示：之前硬编码永远写 report.md，
                    但 6 维/AI 评分实际可能是从 report.md（regex 抽取） 或 export_data.json（结构化兜底） 来的。
                    硬编码会误导用户（明明数据来自 export_data.json，UI 却说是 report.md）。
                    现在的逻辑：six_dim_source / ai_score_source 告诉用户数据实际来源。 #}
                {% set _src6 = achievements.six_dim_source or 'unknown' %}
                {% set _src_ai = achievements.ai_score_source or 'unknown' %}
                {% set _src_text = '' %}
                {% if _src6 == 'report_md' and _src_ai == 'report_md' %}
                    {% set _src_text = 'report.md' %}
                {% elif _src6 == 'report_md' %}
                    {% set _src_text = 'report.md（6 维）+ export_data.json（AI 评分）' %}
                {% elif _src6 == 'export_data' and _src_ai == 'export_data' %}
                    {% set _src_text = 'export_data.json（AI 报告 6 维未识别，回退到结构化数据）' %}
                {% elif _src6 == 'export_data' %}
                    {% set _src_text = 'export_data.json' %}
                {% else %}
                    {% set _src_text = 'report.md / export_data.json（混合）' %}
                {% endif %}
                <div class="text-[10px] text-gray-400 mt-2" title="6 维来源={{ _src6 }}，AI 评分来源={{ _src_ai }}">数据来源：{{ achievements.report_dir }} / {{ _src_text }}</div>
                {% endif %}
            </div>
            {% endif %}
        </div>

        {# v3.9.31 · 家长端入口（除「生成报告」外的第二个入口）
            之前家长订阅版只在生成报告完成页有入口，学员中心 (/me/<uid>) 看不到。
            现在在"个人成就"卡片下方加一行 4 入口快捷区：
            1) 家长订阅版 — /me/<uid>/parent-subscribe
            2) 我的 AI 报告 — /me/<uid>/report-data/<latest> 或 list
            3) 错题集 — /me/<uid>/mistakes（如果有）
            4) 我的二维码海报 — 跳到 /r/<uid> 公开预览页（家长扫码会到的页面）#}
        <div class="bg-white rounded-2xl shadow p-5 mb-4" id="parent-entry">
            <h2 class="text-lg font-bold text-gray-800 mb-3">👨‍👩‍👧 家长与分享</h2>
            <div class="grid grid-cols-2 md:grid-cols-4 gap-3 text-center text-sm">
                <a href="/me/{{ token }}/parent-subscribe" class="block bg-gradient-to-br from-amber-50 to-orange-50 border border-amber-200 rounded-xl p-3 hover:shadow">
                    <div class="text-2xl mb-1">📨</div>
                    <div class="font-semibold text-amber-700">家长订阅版</div>
                    <div class="text-[10px] text-gray-500 mt-1">5 维度决策支持</div>
                </a>
                <a href="/r/{{ token }}" target="_blank" class="block bg-gradient-to-br from-rose-50 to-pink-50 border border-rose-200 rounded-xl p-3 hover:shadow">
                    <div class="text-2xl mb-1">📱</div>
                    <div class="font-semibold text-rose-700">我的二维码</div>
                    <div class="text-[10px] text-gray-500 mt-1">家长扫码预览</div>
                </a>
                <a href="/r/{{ token }}" target="_blank" class="block bg-gradient-to-br from-sky-50 to-blue-50 border border-sky-200 rounded-xl p-3 hover:shadow">
                    <div class="text-2xl mb-1">🤖</div>
                    <div class="font-semibold text-sky-700">AI 讲题入口</div>
                    <div class="text-[10px] text-gray-500 mt-1">每题点开 → aijiangti.cn</div>
                </a>
                <a href="/me/{{ token }}" class="block bg-gradient-to-br from-emerald-50 to-green-50 border border-emerald-200 rounded-xl p-3 hover:shadow">
                    <div class="text-2xl mb-1">🎓</div>
                    <div class="font-semibold text-emerald-700">学员中心</div>
                    <div class="text-[10px] text-gray-500 mt-1">返回个人中心</div>
                </a>
            </div>
            {# v3.9.31 · 如果是家长订阅会员（v3.9.26 门控），给出提示 #}
            {% if has_parent_sub %}
            <div class="mt-3 text-xs text-emerald-700 bg-emerald-50 border border-emerald-200 rounded p-2">
                ✅ 家长订阅会员已开通 · 家长订阅版所有内容可读
            </div>
            {% else %}
            <div class="mt-3 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded p-2">
                💡 家长订阅版目前是预览态 · 完整版需 <a href="/redeem" class="underline">兑换家长订阅码</a>
            </div>
            {% endif %}
        </div>

        <!-- v3.9.7 · 历史报告（从原学员版 /report/student 合并过来） -->
        <div class="bg-white rounded-2xl shadow p-5 mb-4" id="history-reports">
            <div class="flex items-center justify-between mb-3">
                <h2 class="text-lg font-bold text-gray-800">📄 历史报告</h2>
                <span class="text-xs text-gray-400">共 {{ report_htmls|length }} 份</span>
            </div>
            {% if report_htmls %}
            <div class="space-y-2">
                {% for r in report_htmls %}
                <div class="flex items-center justify-between border border-gray-200 rounded-lg p-3 hover:bg-gray-50">
                    <div class="flex-1 min-w-0">
                        <div class="flex items-center gap-2">
                            <span class="text-sm font-bold text-emerald-700">📅 {{ r.mtime_display }}</span>
                            {% if loop.first %}<span class="text-[10px] px-1.5 py-0.5 bg-emerald-100 text-emerald-700 rounded">最新</span>{% endif %}
                            {% if r.has_poster %}<span class="text-[10px] px-1.5 py-0.5 bg-rose-100 text-rose-700 rounded">海报已生成</span>{% endif %}
                            {# v3.9.18 · 半完成状态：AI 报告未生成，但 export_data 完整。给出「数据预览」入口 #}
                            {% if r.status == "data_only" %}
                            <span class="text-[10px] px-1.5 py-0.5 bg-amber-100 text-amber-700 rounded" title="报告数据已抓取（6 维评分/错题/难度分布），AI 报告未生成">📦 数据已抓取</span>
                            {% endif %}
                        </div>
                        <div class="text-[11px] text-gray-400 mt-0.5 truncate">{{ r.dir_name }} · {{ r.size_kb }} KB</div>
                    </div>
                    <div class="flex items-center gap-1.5 ml-2">
                        {% if r.status == "complete" and r.html_url %}
                        <a href="{{ r.html_url }}" target="_blank"
                           class="px-2.5 py-1.5 rounded-md bg-emerald-50 hover:bg-emerald-100 text-emerald-700 text-xs font-bold">🔍 查看</a>
                        {% elif r.status == "data_only" %}
                        <a href="/me/{{ token }}/report-data/{{ r.dir_name }}" target="_blank"
                           class="px-2.5 py-1.5 rounded-md bg-amber-50 hover:bg-amber-100 text-amber-700 text-xs font-bold">📊 数据预览</a>
                        {% else %}
                        <span class="text-[10px] text-gray-400">无报告</span>
                        {% endif %}
                    </div>
                </div>
                {% endfor %}
            </div>
            {% else %}
            <div class="text-center py-4 text-sm text-gray-400">🌱 暂无历史报告</div>
            {% endif %}
        </div>

        <!-- v3.9.7 · 下一步行动（从原学员版合并） -->
        {# v3.9.18 · 改读 student_dict.gesp_highest_passed（视图层已做三层兜底），避免 progress.student.gesp_latest_level 字段不存在 #}
        {% set gesp_level = student.gesp_highest_passed or 0 %}
        {% set gesp_score = student.gesp_latest_score or 0 %}
        {% set next_level = (progress.next_eligible_level or (gesp_level + 1)) if progress else 1 %}
        {% set exemptions = (progress.exemptions or []) if progress else [] %}
        <div class="bg-white rounded-2xl shadow p-5 mb-4" id="next-action">
            <h2 class="text-lg font-bold text-gray-800 mb-3">🎯 下一步行动</h2>
            <div class="space-y-2">
                {% if gesp_level == 0 %}
                <div class="flex items-start gap-3 p-3 bg-emerald-50 rounded-lg">
                    <span class="text-2xl">🚀</span>
                    <div class="flex-1">
                        <div class="font-bold text-sm text-emerald-800">建议先报 GESP 1 级</div>
                        <div class="text-xs text-emerald-600 mt-0.5">从 1 级开始是硬规则 · 通过后 90+ 可跳级</div>
                    </div>
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

        <!-- v3.6 · 错题集（点击 AI 讲题 → 直接传错题给 StudyMate） -->
        <div class="bg-white rounded-2xl shadow p-5 mb-4" id="mistakes">
            <div class="flex items-center justify-between mb-3 flex-wrap gap-2">
                <h2 class="text-lg font-bold text-gray-800">📚 我的错题集</h2>
                {# v3.9.19 · 显示"显示 N / 共 M 道"，让用户知道有更多错题可展开 #}
                <span class="text-xs text-gray-500">
                    显示 {{ mistake_count }}{% if achievements.total_mistakes and achievements.total_mistakes > mistake_count %} / 共 {{ achievements.total_mistakes }} 道{% endif %}
                    · 来自最新 AI 报告
                </span>
            </div>

            {% if mistake_count > 0 %}
            <div id="mistakes-list" class="space-y-2 max-h-[480px] overflow-y-auto pr-1">
                {% for m in achievements.mistakes %}
                <div class="mistake-item border border-gray-200 rounded-lg p-3 hover:border-emerald-300 transition{% if loop.index0 >= 5 %} extra-mistake{% endif %}">
                    <div class="flex items-start justify-between gap-2 flex-wrap">
                        <div class="min-w-0 flex-1">
                            <div class="flex items-center gap-1.5 flex-wrap text-sm">
                                <span class="text-gray-400 font-mono text-xs">#{{ m.idx }}</span>
                                {% if m.problem_id %}
                                <a href="https://www.luogu.com.cn/problem/{{ m.problem_id }}" target="_blank" class="font-bold text-blue-700 hover:underline">{{ m.problem_id }}</a>
                                {% endif %}
                                <span class="font-bold text-gray-800 truncate">{{ m.title }}</span>
                                {% if m.source %}<span class="text-[10px] px-1.5 py-0.5 bg-purple-100 text-purple-700 rounded">{{ m.source }}</span>{% endif %}
                            </div>
                            {% if m.summary %}
                            <div class="text-xs text-gray-600 mt-1.5 line-clamp-2">💡 {{ m.summary }}</div>
                            {% endif %}
                            {% if m.bottleneck %}
                            <div class="text-xs text-red-600 mt-1">⚠️ {{ m.bottleneck[:120] }}{% if m.bottleneck|length > 120 %}…{% endif %}</div>
                            {% endif %}
                        </div>
                        <!-- v3.9.9 · 直跳 aijiangti.cn · C++ 课件生成（题号/标题/来源已直传） -->
                        <a href="https://aijiangti.cn/?pid={{ m.problem_id }}&from=luogu&lang=cpp&require={{ '用C++代码实现并讲解'|urlencode }}&source={{ (m.source or '')|urlencode }}&title={{ (m.title or '')|urlencode }}"
                           target="_blank" rel="noopener"
                           class="flex-shrink-0 inline-flex items-center gap-1 px-3 py-1.5 bg-gradient-to-r from-blue-500 to-cyan-500 text-white text-xs font-bold rounded-lg hover:from-blue-600 hover:to-cyan-600 whitespace-nowrap"
                           title="跳到 aijiangti.cn 生成 C++ 课件（题目已传入）">
                            🤖 AI 讲题
                        </a>
                    </div>
                </div>
                {% endfor %}
            </div>
            {# v3.9.19 · 「折叠」切换按钮，错题 > 5 道才显示。默认全部展开 #}
            {% if achievements.mistakes|length > 5 %}
            <div class="text-center mt-3">
                <button id="mistakes-toggle-btn" onclick="toggleMistakesMain()" type="button"
                        class="text-xs px-4 py-1.5 bg-emerald-50 hover:bg-emerald-100 text-emerald-700 rounded-md font-bold">
                    折叠错题（仅看前 5 道）
                </button>
            </div>
            <style>
                #mistakes-list.collapsed .extra-mistake { display: none; }
            </style>
            <script>
                function toggleMistakesMain() {
                    var list = document.getElementById('mistakes-list');
                    var btn = document.getElementById('mistakes-toggle-btn');
                    if (!list || !btn) return;
                    list.classList.toggle('collapsed');
                    if (list.classList.contains('collapsed')) {
                        btn.textContent = '展开全部错题（{{ achievements.mistakes|length }} 道）';
                    } else {
                        btn.textContent = '折叠错题（仅看前 5 道）';
                    }
                }
            </script>
            {% endif %}
            <p class="text-[10px] text-gray-400 mt-2">💡 点击「AI 讲题」直跳 aijiangti.cn · 自动用 C++ 代码实现并讲解（题号 / 标题 / 来源已直传）</p>
            {% else %}
            <div class="text-center py-6 text-sm text-gray-400">
                🌱 暂无错题记录
                <div class="text-xs mt-1">{% if achievements.report_dir %}最新报告 {{ achievements.report_dir }} 未抽取到错题{% else %}请先在首页生成一份 AI 报告{% endif %}</div>
            </div>
            {% endif %}
        </div>

        <!-- v3.9.21 · 简化为「查看海报」入口（点开才看大图），不再内嵌 200KB PNG 占首屏 -->
        <div class="bg-gradient-to-r from-emerald-50 to-teal-50 border border-emerald-200 rounded-2xl shadow p-4 mb-4">
            <div class="flex items-center justify-between gap-3 flex-wrap">
                <div class="flex items-center gap-2 min-w-0">
                    <span class="text-2xl">🖼️</span>
                    <div class="min-w-0">
                        <div class="text-base font-bold text-gray-800 truncate">学习报告位置图</div>
                        <div class="text-xs text-gray-500">海报 · 扫码直达你的位置图</div>
                    </div>
                </div>
                <div class="flex items-center gap-2 flex-wrap">
                    {# v3.9.23 · STUDENT_ME_HTML 用 token 而非 luogu_uid（luogu_uid 在此模板里未传入，渲染为 ""） #}
                    <a href="/me/{{ token }}/share-card.png" target="_blank"
                       class="inline-flex items-center gap-1.5 px-4 py-2 bg-emerald-600 text-white text-sm font-bold rounded-lg hover:bg-emerald-700 whitespace-nowrap">
                        🔍 查看海报
                    </a>
                    <a href="/me/{{ token }}/share-card.png" download="学习报告位置图_{{ student.real_name or token }}.png"
                       class="inline-flex items-center gap-1.5 px-4 py-2 bg-white border border-emerald-600 text-emerald-700 text-sm font-bold rounded-lg hover:bg-emerald-50 whitespace-nowrap">
                        💾 保存
                    </a>
                </div>
            </div>
        </div>

        {# v3.9.21 · 已删除「AI 讲题·需家长订阅」整块（用户不需要营销模块）。 #}

        <div class="text-center text-xs text-gray-400 mt-6 mb-4">
            v3.5.2 学员 Pro 自助入口 · 基于洛谷 UID 直链（无密码模式）<br>
            真实部署时将改为微信扫码 / 短信 OTP（v3.5.3）
        </div>
    </div>
</body>
</html>
"""


REPORT_PREVIEW_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<meta name="robots" content="noindex">
<meta property="og:type" content="article">
<meta property="og:title" content="{{ student_name }} 的洛谷 AI 测评报告">
{# v3.9.23 · STUDENT_ME_HTML 用 token 而非 luogu_uid（luogu_uid 在此模板里未传入，渲染为 ""） #}
<meta property="og:image" content="/me/{{ token }}/share-card.png">
<title>{{ student_name }} 的洛谷 AI 测评报告</title>
<script src="https://cdn.tailwindcss.com"></script>
{{ app_skin_head() }}
<style>
.glass { backdrop-filter: blur(8px); background: rgba(255,255,255,0.85); }
</style>
</head>
<body class="app-body min-h-screen">

<header class="sticky top-0 z-40 glass border-b border-gray-200">
  <div class="max-w-[480px] mx-auto px-4 py-3 flex items-center justify-between">
    <div class="text-sm font-bold text-emerald-700">🌱 洛谷 AI 测评</div>
    <a href="/?ref={{ ref or '' }}" class="text-xs bg-emerald-600 text-white px-3 py-1.5 rounded-full font-bold">✨ 免费生成</a>
  </div>
</header>

<main class="max-w-[480px] mx-auto px-4 py-5">

  {% if not has_report %}
  <div class="bg-white rounded-2xl shadow p-8 text-center mt-8">
    <div class="text-5xl mb-3">📭</div>
    <h1 class="text-lg font-bold text-gray-800 mb-2">该选手暂未生成报告</h1>
    <p class="text-sm text-gray-500 mb-5">UID {{ token }} · 暂无 AI 测评数据</p>
    <a href="/?ref={{ ref or '' }}" class="inline-block px-5 py-2.5 bg-emerald-600 text-white text-sm font-bold rounded-lg">✨ 立即生成我的报告</a>
  </div>
  {% else %}

  <section class="bg-gradient-to-br from-emerald-50 via-white to-amber-50 rounded-2xl shadow p-5 text-center">
    <div class="text-xs text-gray-500 mb-1">洛谷 UID</div>
    <div class="text-2xl font-extrabold text-gray-800 mb-3 font-mono">{{ token }}</div>
    <div class="text-xs text-amber-700 font-bold mb-1">AI 评测分</div>
    <div class="text-5xl font-extrabold text-amber-600 my-2">
      {{ achievements.ai_score_thousand if achievements.ai_score_thousand is not none else '—' }}
      <span class="text-base text-gray-500 font-normal">/1000</span>
    </div>
    <div class="text-sm font-bold text-amber-700 px-2 break-words leading-snug">{{ achievements.ai_score_label or '—' }}</div>
    <div class="grid grid-cols-3 gap-2 mt-4 text-center text-xs">
      <div class="bg-white/60 rounded-lg p-2"><div class="text-gray-500">错题</div><div class="text-base font-bold text-red-600 mt-0.5">{{ achievements.mistakes|length }}</div></div>
      <div class="bg-white/60 rounded-lg p-2">
        <div class="text-gray-500">GESP 段位</div>
        {# v3.9.29 · 之前硬编码"—"，gasp 段位来自 student.gesp_highest_passed（admin 录入） #}
        {% if gesp_level and gesp_level > 0 %}
          <div class="text-base font-bold text-emerald-600 mt-0.5">{{ gesp_level }} 级{% if gesp_score %} · {{ gesp_score }} 分{% endif %}</div>
        {% else %}
          <div class="text-base font-bold text-emerald-600 mt-0.5">未录入</div>
        {% endif %}
      </div>
      <div class="bg-white/60 rounded-lg p-2">
        <div class="text-gray-500">能力维度</div>
        {# v3.9.29 · 改 6 维来源标签：自 report.md vs export_data.json #}
        <div class="text-base font-bold text-blue-600 mt-0.5">
          {{ achievements.six_dim|length }} 维
          {% if achievements.six_dim_source == 'export_data' %}<span class="text-[10px] text-amber-600">（兜底）</span>{% endif %}
        </div>
      </div>
    </div>
  </section>

  {% if ai_summary %}
  <section class="mt-4 bg-purple-50 border border-purple-200 rounded-2xl p-4">
    <div class="text-xs text-purple-700 font-bold mb-1.5">💡 AI 一句话总结</div>
    <p class="text-sm text-gray-700 leading-relaxed">{{ ai_summary }}</p>
  </section>
  {% endif %}

  {% if achievements.six_dim %}
  <section class="mt-4 bg-white rounded-2xl shadow p-5">
    <h2 class="text-sm font-bold text-gray-800 mb-3">📊 6 维能力评分</h2>
    <div class="space-y-2">
      {% for k, v in achievements.six_dim.items() %}
      <div class="flex items-center gap-2 text-xs">
        <div class="w-20 text-gray-600 text-right">{{ k }}</div>
        <div class="flex-1 bg-gray-100 rounded-full h-2.5 overflow-hidden">
          <div class="h-full rounded-full {% if v >= 75 %}bg-green-500{% elif v >= 55 %}bg-emerald-400{% elif v >= 40 %}bg-amber-400{% else %}bg-red-400{% endif %}"
               style="width: {{ v }}%"></div>
        </div>
        <div class="w-10 text-right font-mono font-bold {% if v >= 75 %}text-green-700{% elif v >= 55 %}text-emerald-700{% elif v >= 40 %}text-amber-700{% else %}text-red-700{% endif %}">{{ v }}</div>
      </div>
      {% endfor %}
    </div>
  </section>
  {% endif %}

  {% if achievements.mistakes %}
  <section class="mt-4 bg-white rounded-2xl shadow p-5">
    <h2 class="text-sm font-bold text-gray-800 mb-3">🎯 错题本预览（Top {{ achievements.mistakes[:3]|length }}）</h2>
    <div class="space-y-2">
      {% for m in achievements.mistakes[:3] %}
      <div class="border border-gray-200 rounded-lg p-2.5">
        <div class="flex items-center gap-1.5 flex-wrap text-xs">
          <span class="text-gray-400 font-mono">#{{ m.idx }}</span>
          {% if m.problem_id %}<span class="font-bold text-blue-700">{{ m.problem_id }}</span>{% endif %}
          <span class="font-bold text-gray-800">{{ m.title }}</span>
          {% if m.source %}<span class="text-[10px] px-1 py-0.5 bg-purple-100 text-purple-700 rounded">{{ m.source }}</span>{% endif %}
        </div>
        {% if m.summary %}<div class="text-xs text-gray-600 mt-1">💡 {{ m.summary[:60] }}{% if m.summary|length > 60 %}…{% endif %}</div>{% endif %}
        {# v3.9.9 · /r/<uid> 预览区补 AI 讲题入口（直跳 aijiangti.cn，C++ 课件生成） #}
        {% if m.problem_id %}
        <a href="https://aijiangti.cn/?pid={{ m.problem_id }}&from=luogu&lang=cpp&require={{ '用C++代码实现并讲解'|urlencode }}&source={{ (m.source or '')|urlencode }}&title={{ (m.title or '')|urlencode }}"
           target="_blank" rel="noopener"
           class="inline-flex items-center gap-1 mt-1.5 px-2.5 py-1 bg-gradient-to-r from-blue-500 to-cyan-500 text-white text-[11px] font-bold rounded-md hover:from-blue-600 hover:to-cyan-600"
           title="跳到 aijiangti.cn 生成 C++ 课件（题目已传入）">
          🤖 AI 讲题
        </a>
        {% endif %}
      </div>
      {% endfor %}
    </div>
  </section>
  {% endif %}

  {% if suggestions %}
  <section class="mt-4 bg-gradient-to-br from-amber-50 to-yellow-50 border border-amber-200 rounded-2xl p-4">
    <h2 class="text-sm font-bold text-amber-800 mb-2">🎯 训练建议</h2>
    <ul class="space-y-1.5 text-sm text-gray-700">
      {% for s in suggestions %}
      <li class="flex gap-2"><span class="text-amber-600 font-bold">✓</span><span>{{ s }}</span></li>
      {% endfor %}
    </ul>
  </section>
  {% endif %}

  <section class="mt-5 grid grid-cols-1 md:grid-cols-2 gap-3">
    <a href="/reports/{{ latest_dir_name }}/report.html" target="_blank"
       class="block bg-white border-2 border-emerald-600 rounded-xl p-4 text-center hover:bg-emerald-50">
      <div class="text-2xl mb-1">🔍</div>
      <div class="text-sm font-bold text-emerald-700">看完整 AI 报告</div>
      <div class="text-[10px] text-gray-500 mt-1">在新窗口打开 · HTML 版</div>
    </a>
    <a href="/?ref={{ ref or luogu_uid }}"
       class="block bg-gradient-to-br from-emerald-500 to-teal-600 rounded-xl p-4 text-center text-white shadow-lg hover:from-emerald-600">
      <div class="text-2xl mb-1">✨</div>
      <div class="text-sm font-bold">生成你的报告</div>
      <div class="text-[10px] text-emerald-100 mt-1">3 分钟拿到 AI 测评</div>
    </a>
  </section>

  {% endif %}

  <section class="mt-6 text-center text-xs text-gray-400">
    <p>🌱 已为 100+ 位信竞家长提供 AI 测评服务</p>
    <p class="mt-1">家长分享 · 报告内容仅展示 UID，不含个人隐私</p>
  </section>

  <footer class="mt-6 pt-4 border-t border-gray-200 text-center text-xs text-gray-400 pb-24">
    <a href="/" class="hover:text-emerald-600 mx-2">首页</a>·
    <a href="/about" class="hover:text-emerald-600 mx-2">关于</a>·
    <a href="/privacy" class="hover:text-emerald-600 mx-2">隐私</a>
    <p class="mt-2">© 2026 洛谷 AI 测评 · 让数据帮孩子少走弯路</p>
  </footer>
</main>

{% if has_report %}
<div class="fixed bottom-0 inset-x-0 z-30 md:hidden bg-white/95 backdrop-blur border-t border-gray-200 p-3 shadow-2xl">
  <a href="/?ref={{ ref or luogu_uid }}"
     class="block w-full text-center px-5 py-3 bg-gradient-to-r from-emerald-500 to-teal-600 text-white text-sm font-bold rounded-xl">
    ✨ 免费生成我的报告（3 分钟）
  </a>
</div>
{% endif %}

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
    {{ app_skin_head() }}
</head>
<body class="app-body p-6 flex items-center justify-center min-h-screen">
    <div class="app-card max-w-md w-full text-center">
        <div class="text-5xl mb-3">⚠️</div>
        <h1 class="app-title">{{ message }}</h1>
        <p class="app-subtitle">一次性填写 · 注册 + 生成报告</p>
        <a href="/generate-form" class="app-btn app-btn-primary mt-4">🚀 去生成学习报告（含注册）</a>
        <p class="mt-3 text-xs text-gray-500">
            <a href="/" class="app-link">← 返回首页</a>
        </p>
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
    try:
        from docs.gesp_estimator import is_csp_age_eligible
    except Exception:
        from gesp_estimator import is_csp_age_eligible
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

# v3.9.8 · 公开的「升学政策库」浏览页（家长端可直接看，无需登录）
# 路由：/policies?city=北京  →  按城市筛
#       /policies?type=tech_talent_junior&city=长沙  →  按类型 + 城市
@app.route("/policies")
def public_policy_browser():
    """v3.9.8 · 家长公开查询的升学政策库

    之前政策数据只放在 /admin/policies（admin 后台），
    家长只能等"AI 家长报告"生成时被动看到，无法主动查阅。
    现在家长可在 /policies 按城市 + 学段 + 类型自己翻阅。
    """
    from task_store import _get_conn
    # 拿查询参数
    city = (request.args.get("city") or "").strip()
    province = (request.args.get("province") or "").strip()
    school_type = (request.args.get("type") or "").strip()
    stage = (request.args.get("stage") or "").strip()  # primary/junior/senior

    conn = _get_conn()
    try:
        # 总览统计
        stats = {
            "total": conn.execute("SELECT COUNT(*) FROM policy_match_schools").fetchone()[0],
            "cities": conn.execute(
                "SELECT COUNT(DISTINCT city) FROM policy_match_schools WHERE city != '全国'"
            ).fetchone()[0],
            "tech_talent": conn.execute(
                "SELECT COUNT(*) FROM policy_match_schools WHERE school_type='tech_talent_junior'"
            ).fetchone()[0],
            "self_enroll": conn.execute(
                "SELECT COUNT(*) FROM policy_match_schools WHERE school_type='self_enroll_senior'"
            ).fetchone()[0],
            "qiangji": conn.execute(
                "SELECT COUNT(*) FROM policy_match_schools WHERE school_type='qiangji_university'"
            ).fetchone()[0],
        }
        # 城市列表（去重，按拼音/字排序）
        city_rows = conn.execute(
            "SELECT DISTINCT city, province FROM policy_match_schools WHERE city != '全国' "
            "ORDER BY city"
        ).fetchall()
        cities = [dict(r) for r in city_rows]

        # 实际查询
        where = ["1=1"]
        params = []
        if city:
            where.append("(city = ? OR province = ?)")
            params.extend([city, city])
        if school_type:
            where.append("school_type = ?")
            params.append(school_type)
        if stage:
            where.append("target_stage = ?")
            params.append(stage)
        sql = (
            "SELECT * FROM policy_match_schools WHERE " + " AND ".join(where)
            + " ORDER BY school_type, priority ASC, school_name"
        )
        rows = conn.execute(sql, params).fetchall()
        policies = [dict(r) for r in rows]
    finally:
        conn.close()

    return render_template_string(
        PUBLIC_POLICIES_HTML,
        policies=policies,
        cities=cities,
        stats=stats,
        city=city,
        province=province,
        school_type=school_type,
        stage=stage,
        type_labels={
            "tech_talent_junior": "科技特长生（初中）",
            "self_enroll_senior": "自招/特长生（高中）",
            "qiangji_university": "强基计划（大学）",
        },
        stage_labels={"primary": "小学", "junior": "初中", "senior": "高中"},
    )


ADMIN_STUDENTS_GUARDIANS_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>家长列表 - {{ student.real_name or ('UID-' + student.luogu_uid) }}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    {{ app_skin_head() }}
</head>
<body class="app-body p-6">
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
    {{ app_skin_head() }}
</head>
<body class="app-body p-6">
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
    {{ app_skin_head() }}
</head>
<body class="app-body p-6">
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
    {{ app_skin_head() }}
    <style>
        .card-shadow { box-shadow: 0 4px 16px rgba(0,0,0,0.06); }
    </style>
</head>
<body class="app-body min-h-screen">
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
                <div class="flex flex-col items-end gap-1">
                    <span class="text-xs px-2 py-0.5 bg-emerald-100 text-emerald-700 rounded-full">v3.9.8</span>
                    {# v3.9.8 · 家长可主动查全国政策库 #}
                    <a href="/policies?city={{ student.city|urlencode }}" target="_blank"
                       class="text-xs text-emerald-600 hover:underline">📖 查看全国政策库 →</a>
                </div>
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
    {{ app_skin_head() }}
</head>
<body class="app-body min-h-screen flex items-center justify-center p-6">
    <div class="bg-white rounded-xl shadow p-8 max-w-md w-full text-center">
        <h1 class="text-2xl font-bold text-red-700 mb-3">⚠️ 链接无效</h1>
        <p class="text-gray-600 mb-4">{{ message }}</p>
        <p class="text-xs text-gray-400">请联系您的教练重新获取家长链接</p>
    </div>
</body>
</html>
"""


# v3.9.8 · 家长公开查询的升学政策库（HTML 模板）
PUBLIC_POLICIES_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🏫 升学政策库 · 全国省会/直辖市</title>
    <script src="https://cdn.tailwindcss.com"></script>
    {{ app_skin_head() }}
    <style>
        .policy-card { transition: all .15s; }
        .policy-card:hover { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(0,0,0,.08); }
    </style>
</head>
<body class="app-body">
    <div class="max-w-5xl mx-auto p-4 sm:p-6 space-y-4">
        <!-- 顶部标题 -->
        <div class="bg-gradient-to-r from-emerald-600 to-teal-600 rounded-2xl p-5 text-white shadow-lg">
            <div class="flex items-start justify-between gap-3">
                <div>
                    <h1 class="text-2xl font-bold">🏫 全国升学政策库</h1>
                    <p class="text-sm opacity-90 mt-1">v3.9.8 · 覆盖 {{ stats.cities }} 个城市 · {{ stats.total }} 所样板学校</p>
                </div>
                <a href="/" class="text-xs bg-white/20 hover:bg-white/30 px-3 py-1.5 rounded-lg">← 返回首页</a>
            </div>
            <div class="mt-3 grid grid-cols-3 gap-2 text-center text-xs">
                <div class="bg-white/15 rounded-lg p-2">
                    <div class="font-bold text-base">{{ stats.tech_talent }}</div>
                    <div class="opacity-80">科技特长生（初中）</div>
                </div>
                <div class="bg-white/15 rounded-lg p-2">
                    <div class="font-bold text-base">{{ stats.self_enroll }}</div>
                    <div class="opacity-80">自招高中</div>
                </div>
                <div class="bg-white/15 rounded-lg p-2">
                    <div class="font-bold text-base">{{ stats.qiangji }}</div>
                    <div class="opacity-80">强基大学</div>
                </div>
            </div>
        </div>

        <!-- 筛选器 -->
        <form method="GET" action="/policies" class="bg-white rounded-2xl shadow p-4">
            <div class="grid grid-cols-1 md:grid-cols-4 gap-3">
                <div>
                    <label class="text-xs text-gray-500 font-bold">🏙️ 城市/省份</label>
                    <select name="city" class="mt-1 w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
                        <option value="">— 全部城市 —</option>
                        {% for c in cities %}
                        <option value="{{ c.city }}" {% if city == c.city %}selected{% endif %}>
                            {{ c.city }}{% if c.city != c.province %}（{{ c.province }}）{% endif %}
                        </option>
                        {% endfor %}
                    </select>
                </div>
                <div>
                    <label class="text-xs text-gray-500 font-bold">🎓 当前学段</label>
                    <select name="stage" class="mt-1 w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
                        <option value="">— 全部学段 —</option>
                        {% for k, v in stage_labels.items() %}
                        <option value="{{ k }}" {% if stage == k %}selected{% endif %}>{{ v }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div>
                    <label class="text-xs text-gray-500 font-bold">📋 政策类型</label>
                    <select name="type" class="mt-1 w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
                        <option value="">— 全部类型 —</option>
                        {% for k, v in type_labels.items() %}
                        <option value="{{ k }}" {% if school_type == k %}selected{% endif %}>{{ v }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div class="flex items-end gap-2">
                    <button type="submit" class="flex-1 bg-emerald-600 hover:bg-emerald-700 text-white font-bold px-4 py-2 rounded-lg text-sm">
                        🔍 筛选
                    </button>
                    <a href="/policies" class="bg-gray-200 hover:bg-gray-300 text-gray-700 font-bold px-3 py-2 rounded-lg text-sm">重置</a>
                </div>
            </div>
            <p class="text-xs text-gray-500 mt-2">
                💡 提示：家长报告第 3 章「OI 决策支持（升学/政策窗口）」自动引用本库数据。
                找到匹配目标后，请务必到目标校官网或当地教育局核实当年最新简章。
            </p>
        </form>

        <!-- 政策卡片列表 -->
        <div class="space-y-2">
            {% if policies %}
                {% for p in policies %}
                <div class="policy-card bg-white rounded-xl shadow border border-gray-200 p-4 border-l-4
                    {% if p.school_type == 'tech_talent_junior' %}border-l-emerald-500
                    {% elif p.school_type == 'self_enroll_senior' %}border-l-blue-500
                    {% else %}border-l-purple-500{% endif %}">
                    <div class="flex items-start justify-between gap-3">
                        <div class="flex-1 min-w-0">
                            <div class="flex items-center flex-wrap gap-2">
                                <span class="text-base font-bold text-gray-800">{{ loop.index }}. {{ p.school_name }}</span>
                                <span class="text-xs px-1.5 py-0.5 rounded
                                    {% if p.school_type == 'tech_talent_junior' %}bg-emerald-100 text-emerald-700
                                    {% elif p.school_type == 'self_enroll_senior' %}bg-blue-100 text-blue-700
                                    {% else %}bg-purple-100 text-purple-700{% endif %}">
                                    {{ type_labels.get(p.school_type, p.school_type) }}
                                </span>
                                <span class="text-xs px-1.5 py-0.5 rounded bg-gray-100 text-gray-600">
                                    {{ stage_labels.get(p.target_stage, p.target_stage) }} →
                                </span>
                                {% if p.is_recommended %}<span class="text-xs px-1.5 py-0.5 rounded bg-amber-100 text-amber-700">⭐ 推荐</span>{% endif %}
                            </div>
                            <div class="text-sm text-gray-700 mt-1.5">📋 {{ p.policy_summary }}</div>
                            <div class="flex gap-3 mt-1.5 text-xs text-gray-500 flex-wrap">
                                <span>👥 招生 {{ p.enrollment_count or '—' }} 人</span>
                                <span>🎯 {{ p.requires_competition or '—' }}</span>
                                <span>📍 {{ p.city }}{% if p.city != p.province %} · {{ p.province }}{% endif %}</span>
                            </div>
                        </div>
                        {% if p.policy_url %}
                        <a href="{{ p.policy_url }}" target="_blank" rel="noopener"
                           class="flex-shrink-0 text-xs text-emerald-600 hover:underline whitespace-nowrap mt-1">
                            查看政策 →
                        </a>
                        {% endif %}
                    </div>
                </div>
                {% endfor %}
            {% else %}
                <div class="bg-white rounded-2xl shadow p-8 text-center text-gray-500">
                    <div class="text-4xl mb-2">🔍</div>
                    <p class="text-sm">未找到匹配的政策</p>
                    <p class="text-xs text-gray-400 mt-1">请尝试更换筛选条件，或直接联系当地教育局</p>
                </div>
            {% endif %}
        </div>

        <p class="text-center text-xs text-gray-400 mt-4">
            数据来源：admin 维护的 policy_match_schools 表 · 升学政策每年可能调整，请以教育局官方简章为准
        </p>
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
    {{ app_skin_head() }}
    <style>
    </style>
</head>
<body class="app-body flex items-center justify-center p-4">
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
