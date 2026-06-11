"""
任务记录持久化存储模块 (SQLite)
重启服务后任务记录不会丢失
"""

import os
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

# 允许通过环境变量覆盖，便于 Docker 命名卷挂目录场景
# 默认：项目根目录下 tasks.db（绝对路径，不受 CWD 影响）
# Docker：通过 TASK_DB_PATH=/app/data/tasks.db 把 db 文件写到挂载的卷里
_DEFAULT_DB_PATH = Path(__file__).resolve().parent / "tasks.db"
DB_PATH = Path(os.environ.get("TASK_DB_PATH", str(_DEFAULT_DB_PATH)))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ----------------------------------------------------------------------------
# 单一可信源：tasks 表除 task_id 外的所有列定义
# ----------------------------------------------------------------------------
# 加新列只需要在这里加一行，init_db() 启动时自动扫 PRAGMA table_info，
# 缺啥补啥（ALTER TABLE ADD COLUMN）。无需再手动维护 ALTER 列表。
#
# 值是完整的 SQL 类型段（含 NOT NULL / DEFAULT）。
#   - "TEXT DEFAULT ''"          → 字符串类字段（默认空串）
#   - "INTEGER DEFAULT 0"        → 计数 / 进度类字段
#   - "TEXT NOT NULL DEFAULT 'queued'" → 状态字段
# ----------------------------------------------------------------------------
TASK_COLUMNS: dict[str, str] = {
    "status":                "TEXT NOT NULL DEFAULT 'queued'",
    "message":               "TEXT DEFAULT ''",
    "html":                  "TEXT DEFAULT ''",
    "pdf":                   "TEXT DEFAULT ''",
    "md":                    "TEXT DEFAULT ''",
    "student_name":          "TEXT DEFAULT ''",
    "school":                "TEXT DEFAULT ''",
    "grade":                 "TEXT DEFAULT ''",
    "solved_count":          "INTEGER DEFAULT 0",
    "failed_count":          "INTEGER DEFAULT 0",
    "eval_time":             "TEXT DEFAULT ''",
    "stage":                 "TEXT DEFAULT ''",
    "source_code_success":   "INTEGER DEFAULT 0",
    "source_code_total":     "INTEGER DEFAULT 0",
    "ai_progress":           "INTEGER DEFAULT 0",
    "ai_elapsed_seconds":    "INTEGER DEFAULT 0",
    "tag_fetch_success":     "INTEGER DEFAULT 0",
    "tag_fetch_total":       "INTEGER DEFAULT 0",
    "retry_form_json":       "TEXT DEFAULT ''",
    "student_id":            "INTEGER REFERENCES students(id)",  # v3.5.2+：关联学员档案
    "created_at":            "TEXT DEFAULT ''",
    # v3.5.2 · 家长订阅版二次生成：任务类型 + 关联 UID + 家长版产物 URL
    "task_type":             "TEXT DEFAULT ''",
    "luogu_uid":             "TEXT DEFAULT ''",
    "ps_html":               "TEXT DEFAULT ''",
    "ps_md":                 "TEXT DEFAULT ''",
}


def _get_conn() -> sqlite3.Connection:
    # v3.8 · 多进程/IDE 自动重启场景下避免 "unable to open database file"
    # · check_same_thread=False: Flask 跨线程访问同一连接
    # · busy_timeout=10000:  写锁被占时最多等 10s，而不是立刻抛 SQLITE_BUSY
    # · journal_mode=WAL:     读写并发不互斥，readers 不阻塞 writer
    # · foreign_keys=ON:      让 _admin_students 等子模块的 FK 约束真正生效
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
    except Exception:
        # WAL 在某些只读 FS / 网络盘下不可用，失败时降级回默认（不致命）
        pass
    return conn


def _build_create_table_sql() -> str:
    """根据 TASK_COLUMNS 生成完整的 CREATE TABLE 语句（首次部署用）"""
    cols = ["task_id TEXT PRIMARY KEY"]
    cols.extend(f"{name} {typedef}" for name, typedef in TASK_COLUMNS.items())
    body = ",\n            ".join(cols)
    return f"CREATE TABLE IF NOT EXISTS tasks (\n            {body}\n        )"


def _ensure_columns(conn: sqlite3.Connection) -> list[str]:
    """对比 PRAGMA table_info 与 TASK_COLUMNS，对缺失列执行 ALTER TABLE ADD COLUMN。
    返回本次新加的列名列表。"""
    actual = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    added: list[str] = []
    for name, typedef in TASK_COLUMNS.items():
        if name not in actual:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {typedef}")
            added.append(name)
    return added


def init_db():
    """初始化数据库表

    升级路径：
      v0 → v1：tasks 表（首版）
      v1 → v2：增量 ALTER tasks（stage / source_code_* / ai_* / retry_form_json）
      v2 → v3：students + student_cookies + tasks.student_id（学员档案）
      v3 → v3.5：4 张赛事核心表 + 4 张业务表 + students 6 个 GESP 字段
    """
    conn = _get_conn()

    # ---- 1. 原始 tasks 表（保持兼容）----
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'queued',
            message TEXT DEFAULT '',
            html TEXT DEFAULT '',
            pdf TEXT DEFAULT '',
            md TEXT DEFAULT '',
            student_name TEXT DEFAULT '',
            school TEXT DEFAULT '',
            grade TEXT DEFAULT '',
            solved_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            eval_time TEXT DEFAULT '',
            stage TEXT DEFAULT '',
            source_code_success INTEGER DEFAULT 0,
            source_code_total INTEGER DEFAULT 0,
            ai_progress INTEGER DEFAULT 0,
            ai_elapsed_seconds INTEGER DEFAULT 0,
            retry_form_json TEXT DEFAULT '',
            created_at TEXT DEFAULT ''
        )
    """)

    # ---- 2. v2 学员档案 ----
    conn.execute("""
        CREATE TABLE IF NOT EXISTS students (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            luogu_uid   TEXT UNIQUE NOT NULL,
            real_name   TEXT,
            school      TEXT,
            grade       TEXT,
            is_minor    BOOLEAN NOT NULL DEFAULT 0,
            guardian_consent_at  DATETIME,
            note        TEXT,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS student_cookies (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id  INTEGER NOT NULL REFERENCES students(id),
            cookies     TEXT,
            source      TEXT,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_student_cookies_student ON student_cookies(student_id)")

    # ---- 3. v3.5 赛事核心 4 表 ----
    conn.execute("""
        CREATE TABLE IF NOT EXISTS competitions (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            code                  TEXT UNIQUE NOT NULL,
            name                  TEXT NOT NULL,
            type                  TEXT NOT NULL,
            level                 INTEGER,
            exam_date             DATE NOT NULL,
            registration_deadline DATE,
            location              TEXT,
            target_audience       TEXT,
            fee_cny               INTEGER DEFAULT 0,
            source_url            TEXT,
            data_year             INTEGER NOT NULL,
            notes                 TEXT,
            updated_at            DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_comp_date ON competitions(exam_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_comp_type_year ON competitions(type, data_year)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS student_competitions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id      INTEGER NOT NULL REFERENCES students(id),
            competition_id  INTEGER NOT NULL REFERENCES competitions(id),
            registered      BOOLEAN DEFAULT 0,
            target_score    INTEGER,
            target_rank     TEXT,
            actual_score    INTEGER,
            actual_rank     TEXT,
            result_level    TEXT,
            notes           TEXT,
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(student_id, competition_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sc_student ON student_competitions(student_id)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS gesp_exams (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id        INTEGER NOT NULL REFERENCES students(id),
            exam_id           INTEGER NOT NULL REFERENCES competitions(id),
            registered_level  INTEGER NOT NULL,
            actual_score      INTEGER,
            passed            BOOLEAN,
            can_skip_next     BOOLEAN DEFAULT 0,
            exempts_csp_j     BOOLEAN DEFAULT 0,
            exempts_csp_s     BOOLEAN DEFAULT 0,
            certificate_no    TEXT,
            notes             TEXT,
            recorded_by       TEXT,
            created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(student_id, exam_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gesp_student_level ON gesp_exams(student_id, registered_level)")

    # ---- 4.6.1 v3.5.3 学员 CSP/NOIP/NOI 历史奖项自录入（CSP初赛 + 复赛 + 获奖年份）----
    conn.execute("""
        CREATE TABLE IF NOT EXISTS csp_awards (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id        INTEGER NOT NULL REFERENCES students(id),
            competition_type  TEXT NOT NULL,        -- 'csp_j_pre' / 'csp_j_final' / 'csp_s_pre' / 'csp_s_final'
                                                -- 'noip_1' / 'noi_bronze' / 'noi_silver' / 'noi_gold'
            award_level       TEXT NOT NULL,        -- 'excellent' / 'first' / 'second' / 'third' / 'bronze' / 'silver' / 'gold'
            award_year        INTEGER NOT NULL,     -- 获奖年份（2020-2030）
            actual_score      INTEGER,              -- 实际分（可选）
            province          TEXT,                 -- 省份（省赛才有，全国赛可空）
            certificate_no    TEXT,                 -- 证书编号
            notes             TEXT,
            recorded_by       TEXT,                 -- 'self'（学员自录）/ 'coach' / 'admin'
            created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(student_id, competition_type, award_year, award_level)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_csp_awards_student ON csp_awards(student_id, award_year)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_csp_awards_type ON csp_awards(competition_type)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS policy_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            event_code      TEXT UNIQUE,
            name            TEXT NOT NULL,
            category        TEXT,
            event_date      DATE,
            target_audience TEXT,
            source_url      TEXT,
            description     TEXT,
            data_year       INTEGER NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_policy_date ON policy_events(event_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_policy_category_year ON policy_events(category, data_year)")

    # v3.5 Phase 3 · 政策日历数据水印（§9 风险对冲）
    try:
        conn.execute("ALTER TABLE policy_events ADD COLUMN last_updated_at DATETIME DEFAULT CURRENT_TIMESTAMP")
    except sqlite3.OperationalError:
        pass

    # ---- 4. v3.5 业务 4 表 ----
    conn.execute("""
        CREATE TABLE IF NOT EXISTS guardians (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id              INTEGER NOT NULL REFERENCES students(id),
            phone                   TEXT,
            email                   TEXT,
            display_name            TEXT,
            notify_channel          TEXT,
            notify_token            TEXT UNIQUE,
            notify_token_expires_at DATETIME,
            consent_ip              TEXT,
            created_at              DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_guardian_student ON guardians(student_id)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS weekly_reports (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id   INTEGER NOT NULL REFERENCES students(id),
            week_start   DATE,
            html_path    TEXT,
            pdf_path     TEXT,
            delivered_at DATETIME,
            open_count   INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_weekly_student ON weekly_reports(student_id, week_start)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS student_goals (
            student_id        INTEGER PRIMARY KEY REFERENCES students(id),
            primary_path      TEXT,
            target_university TEXT,
            target_province   TEXT,
            notes             TEXT,
            updated_at        DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS activation_codes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            code         TEXT UNIQUE NOT NULL,
            sku          TEXT NOT NULL,
            duration_days INTEGER NOT NULL,
            student_id   INTEGER REFERENCES students(id),
            redeemed_at  DATETIME,
            expires_at   DATETIME,
            created_by   TEXT,
            created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_act_code ON activation_codes(code)")

    # ---- 4.5 v3.5 Phase 3 · 冲刺营题库 + 进度 ----
    conn.execute("""
        CREATE TABLE IF NOT EXISTS camp_problems (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            sku        TEXT NOT NULL,         -- 'popularize_camp' / 'improve_camp'
            day        INTEGER NOT NULL,      -- 第几天（1-28 / 1-56）
            pid        TEXT NOT NULL,         -- 洛谷题号
            title      TEXT,
            difficulty INTEGER,
            gesp_level INTEGER,               -- 目标 GESP 等级
            topic      TEXT,
            UNIQUE(sku, day)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_camp_sku_day ON camp_problems(sku, day)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS camp_progress (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id     INTEGER NOT NULL REFERENCES students(id),
            activation_id  INTEGER NOT NULL REFERENCES activation_codes(id),
            sku            TEXT NOT NULL,
            problem_id     INTEGER NOT NULL REFERENCES camp_problems(id),
            submitted      INTEGER DEFAULT 0,
            score          INTEGER,
            submitted_at   TEXT,
            UNIQUE(activation_id, problem_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_camp_prog_student ON camp_progress(student_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_camp_prog_act ON camp_progress(activation_id)")

    # ---- 4.6 v3.5.2 政策匹配学校库（家长版核心模块）----
    # 学段判断：小学 → 当地有科技特长生政策的中学
    #          初中 → 当地有自招政策的高中
    #          高中 → 强基 5 校（清北复交浙）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS policy_match_schools (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            school_name         TEXT NOT NULL,
            school_type         TEXT NOT NULL,        -- 'tech_talent_junior' / 'self_enroll_senior' / 'qiangji_university'
            target_stage        TEXT NOT NULL,        -- 'primary' / 'junior' / 'senior'
            city                TEXT NOT NULL,        -- '北京' / '杭州' （大学为'全国'）
            province            TEXT NOT NULL,        -- '北京' / '浙江'
            policy_summary      TEXT,                 -- '信息学省一 30 分加分'
            enrollment_count    INTEGER,              -- 招生人数
            requires_competition TEXT,                -- 'GESP 7级 80+' / 'CSP-J 一等'
            policy_url          TEXT,                 -- 政策原文链接（占位）
            priority            INTEGER DEFAULT 100,  -- 数值越小越靠前
            effective_year      INTEGER DEFAULT 2026,
            last_updated_at     DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pms_type_city ON policy_match_schools(school_type, city, province)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pms_stage ON policy_match_schools(target_stage)")
    _seed_policy_match_schools(conn)

    # ---- 5. 兼容历史数据库：增量 ALTER（SQLite 不支持 IF NOT EXISTS on ADD COLUMN）----
    alter_ddls = (
        # v1 → v2 旧 tasks 扩展
        "ALTER TABLE tasks ADD COLUMN stage TEXT DEFAULT ''",
        "ALTER TABLE tasks ADD COLUMN source_code_success INTEGER DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN source_code_total INTEGER DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN ai_progress INTEGER DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN ai_elapsed_seconds INTEGER DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN retry_form_json TEXT DEFAULT ''",
        # v2 → v3 tasks 关联学员
        "ALTER TABLE tasks ADD COLUMN student_id INTEGER REFERENCES students(id)",
        # v3 → v3.5 students GESP 6 字段
        "ALTER TABLE students ADD COLUMN gesp_highest_passed INTEGER DEFAULT 0",
        "ALTER TABLE students ADD COLUMN gesp_latest_score INTEGER",
        "ALTER TABLE students ADD COLUMN gesp_can_exempt_csp_j BOOLEAN DEFAULT 0",
        "ALTER TABLE students ADD COLUMN gesp_can_exempt_csp_s BOOLEAN DEFAULT 0",
        "ALTER TABLE students ADD COLUMN gesp_exemption_expiry DATE",
        "ALTER TABLE students ADD COLUMN gesp_next_eligible_level INTEGER",
        # v3.5.2 学员 4 字段极简注册（学而思图 1 模式）
        "ALTER TABLE students ADD COLUMN city TEXT DEFAULT ''",
        "ALTER TABLE students ADD COLUMN gender TEXT DEFAULT ''",
        "ALTER TABLE students ADD COLUMN birth_date DATE",
        "ALTER TABLE students ADD COLUMN registered_via TEXT DEFAULT 'admin'",
        # v3.5.3 学员 GESP 真考记录加获奖年份（4 次/年）
        "ALTER TABLE gesp_exams ADD COLUMN award_year INTEGER",
        # v3.5.3 学员注册时落省份（家长版报告用）
        "ALTER TABLE students ADD COLUMN province TEXT DEFAULT ''",
    )
    for ddl in alter_ddls:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            # 字段已存在，跳过（v1/v2 老库升级时安全）
            pass

    # ---- 4. 兜底：自动补齐 TASK_COLUMNS 里新增但历史建表没写出来的列 ----
    #   解决 tag_fetch_success / tag_fetch_total 等列被新代码引用但老库没有的问题
    added = _ensure_columns(conn)
    if added:
        print(f"[task_store] auto-added columns: {added}")

    conn.commit()
    conn.close()


# ============================================================
# v3.5.2 · 政策匹配学校库（家长版核心模块）
# ============================================================

_POLICY_MATCH_SEED = [
    # ===== 1. 小学 → 当地有科技特长生政策的中学（按城市分组）=====
    # 北京
    ("人大附中早培班", "tech_talent_junior", "primary", "北京", "北京",
     "信息学 CSP-J 一等奖免初试 + 30 分", 80, "CSP-J 一等", "https://www.rdfz.cn/", 10),
    ("北京八中", "tech_talent_junior", "primary", "北京", "北京",
     "科技特长生（信息学）30 分", 40, "CSP-J 二等", "https://www.bj8.org.cn/", 20),
    ("北京十一学校", "tech_talent_junior", "primary", "北京", "北京",
     "科学实验班：信息学特长优先", 60, "GESP 7 级 80+", "https://www.bnds.cn/", 30),
    # 上海
    ("上海中学", "tech_talent_junior", "primary", "上海", "上海",
     "科技实验班：信息学特长 +30 分", 50, "CSP-J 一等", "https://www.shs.cn/", 10),
    ("华育中学", "tech_talent_junior", "primary", "上海", "上海",
     "信息学特长 20 分", 30, "CSP-J 二等", "https://www.hy-school.com/", 20),
    # 杭州
    ("杭州外国语学校", "tech_talent_junior", "primary", "杭州", "浙江",
     "科技特长生：信息学省一 50 分", 40, "GESP 8 级 80+", "https://www.hwfls.com/", 10),
    ("建兰中学", "tech_talent_junior", "primary", "杭州", "浙江",
     "科技特长生：信息学省二 20 分", 30, "GESP 7 级 80+", "https://www.jianlanedu.com/", 20),
    # 深圳
    ("深圳中学", "tech_talent_junior", "primary", "深圳", "广东",
     "创新实验班：信息学特长", 50, "CSP-J 一等", "https://www.shenzhong.net/", 10),
    ("深圳实验学校", "tech_talent_junior", "primary", "深圳", "广东",
     "科技特长生 30 分", 40, "CSP-J 二等", "https://www.szsy.cn/", 20),
    # 成都
    ("成都七中育才学校", "tech_talent_junior", "primary", "成都", "四川",
     "网班：信息学特长优先", 50, "CSP-J 一等", "https://www.cdyucai.com/", 10),
    ("成都石室中学（北湖校区）", "tech_talent_junior", "primary", "成都", "四川",
     "科技特长生 25 分", 30, "CSP-J 二等", "https://www.cd-yucai.cn/", 20),
    # 南京
    ("南京外国语学校", "tech_talent_junior", "primary", "南京", "江苏",
     "科技特长生（信息学）30 分", 40, "CSP-J 一等", "https://www.nfls.com.cn/", 10),
    ("南京树人学校", "tech_talent_junior", "primary", "南京", "江苏",
     "信息学特长 20 分", 30, "CSP-J 二等", "https://www.njshuren.cn/", 20),

    # ===== 2. 初中 → 当地有自招政策的高中 =====
    # 北京
    ("人大附中（ICC）", "self_enroll_senior", "junior", "北京", "北京",
     "自招：信息学省一 30 分", 80, "CSP-S 一等", "https://www.rdfz.cn/", 10),
    ("北京四中", "self_enroll_senior", "junior", "北京", "北京",
     "自招：信息学省一 30 分", 60, "CSP-S 一等", "https://www.bj4hs.cn/", 20),
    ("北京十一学校", "self_enroll_senior", "junior", "北京", "北京",
     "自招：信息学省二 20 分", 50, "CSP-S 二等", "https://www.bnds.cn/", 30),
    # 上海
    ("上海中学", "self_enroll_senior", "junior", "上海", "上海",
     "自招：信息学省一 40 分", 60, "CSP-S 一等", "https://www.shs.cn/", 10),
    ("华师大二附中", "self_enroll_senior", "junior", "上海", "上海",
     "自招：信息学省一 30 分", 50, "CSP-S 一等", "https://www.hsefz.cn/", 20),
    # 杭州
    ("杭州第二中学（滨江校区）", "self_enroll_senior", "junior", "杭州", "浙江",
     "自招：信息学省一 30 分", 80, "CSP-S 一等", "https://www.hz2hs.net.cn/", 10),
    ("学军中学（紫金港校区）", "self_enroll_senior", "junior", "杭州", "浙江",
     "自招：信息学省一 30 分", 60, "CSP-S 一等", "https://www.xjhs.cn/", 20),
    ("杭州外国语学校", "self_enroll_senior", "junior", "杭州", "浙江",
     "自招：信息学省二 20 分", 50, "CSP-S 二等", "https://www.hwfls.com/", 30),
    # 深圳
    ("深圳中学", "self_enroll_senior", "junior", "深圳", "广东",
     "自招：信息学省一 40 分", 60, "CSP-S 一等", "https://www.shenzhong.net/", 10),
    ("深圳实验学校", "self_enroll_senior", "junior", "深圳", "广东",
     "自招：信息学省一 30 分", 50, "CSP-S 一等", "https://www.szsy.cn/", 20),
    # 成都
    ("成都七中（林荫校区）", "self_enroll_senior", "junior", "成都", "四川",
     "自招：信息学省一 30 分", 80, "CSP-S 一等", "https://www.cdqz.net/", 10),
    ("成都石室中学（文庙校区）", "self_enroll_senior", "junior", "成都", "四川",
     "自招：信息学省二 20 分", 50, "CSP-S 二等", "https://www.cd-yucai.cn/", 20),
    # 南京
    ("南京外国语学校", "self_enroll_senior", "junior", "南京", "江苏",
     "自招：信息学省一 30 分", 60, "CSP-S 一等", "https://www.nfls.com.cn/", 10),
    ("南京师范大学附属中学", "self_enroll_senior", "junior", "南京", "江苏",
     "自招：信息学省一 30 分", 50, "CSP-S 一等", "https://www.nsfz.net/", 20),

    # ===== 3. 高中 → 强基 5 校（全国统一）=====
    ("清华大学", "qiangji_university", "senior", "全国", "全国",
     "强基计划：信息学金牌破格入围", 30, "NOI 金牌 / NOIP 省一", "https://www.tsinghua.edu.cn/", 10),
    ("北京大学", "qiangji_university", "senior", "全国", "全国",
     "强基计划：信息学金牌破格入围", 30, "NOI 金牌 / NOIP 省一", "https://www.pku.edu.cn/", 20),
    ("复旦大学", "qiangji_university", "senior", "全国", "全国",
     "强基计划：信息学省一 + 高考一本线", 20, "NOIP 省一 + 高考一本线", "https://www.fudan.edu.cn/", 30),
    ("上海交通大学", "qiangji_university", "senior", "全国", "全国",
     "强基计划：信息学省一 + 高考一本线", 20, "NOIP 省一 + 高考一本线", "https://www.sjtu.edu.cn/", 40),
    ("浙江大学", "qiangji_university", "senior", "全国", "浙江",
     "强基计划：信息学省一 + 高考一本线", 20, "NOIP 省一 + 高考一本线", "https://www.zju.edu.cn/", 50),
]


def _seed_policy_match_schools(conn):
    """种子数据：政策匹配学校库（v3.5.2）

    - 13 所科技特长生中学（6 城：北京/上海/杭州/深圳/成都/南京）
    - 14 所有自招政策的高中（同 6 城）
    - 5 所强基大学（清北复交浙 · 全国）
    - 共 32 所样板学校

    v3.5 反向 Scope 限制：只做样板，后续不扩 39 校强基
    """
    cur = conn.execute("SELECT COUNT(*) FROM policy_match_schools")
    if cur.fetchone()[0] >= len(_POLICY_MATCH_SEED):
        return  # 已种子
    for row in _POLICY_MATCH_SEED:
        try:
            conn.execute(
                """
                INSERT INTO policy_match_schools
                  (school_name, school_type, target_stage, city, province,
                   policy_summary, enrollment_count, requires_competition,
                   policy_url, priority, effective_year)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 2026)
                """,
                row,
            )
        except sqlite3.IntegrityError:
            pass
    conn.commit()


def match_school_for_student(student: dict) -> dict:
    """v3.5.2 政策匹配引擎（家长版核心模块）

    Args:
        student: 必须含 city + grade（v3.5.2 4 字段注册写入）

    Returns:
        {
            'stage': 'primary' | 'junior' | 'senior' | 'college' | 'unknown',
            'stage_label': '小学' / '初中' / '高中' / '大学' / '未识别',
            'match_type': 'tech_talent_junior' | 'self_enroll_senior' | 'qiangji_university' | None,
            'match_type_label': '科技特长生中学' / '自招高中' / '强基大学' / '',
            'city': '北京',
            'province': '北京',
            'matches': [
                {'school_name': '人大附中早培班', 'policy_summary': '...',
                 'enrollment_count': 80, 'requires_competition': 'CSP-J 一等',
                 'policy_url': '...', 'priority': 10, 'is_recommended': True},
                ...
            ],
        }
    """
    if not student:
        return {"stage": "unknown", "matches": []}

    city = (student.get("city") or "").strip()
    grade = (student.get("grade") or "").strip()
    province = _city_to_province_for_match(city) if city else ""

    # 1. 学段判断
    stage, stage_label, match_type, match_type_label = _resolve_match_target(grade)

    if not match_type:
        return {
            "stage": stage,
            "stage_label": stage_label,
            "match_type": None,
            "match_type_label": "",
            "city": city,
            "province": province,
            "matches": [],
        }

    # 2. 查询匹配学校
    conn = _get_conn()
    try:
        # 优先按城市精确匹配，其次按省份
        if match_type == "qiangji_university":
            rows = conn.execute(
                "SELECT * FROM policy_match_schools "
                "WHERE school_type = ? AND effective_year = 2026 "
                "ORDER BY priority ASC",
                (match_type,),
            ).fetchall()
        else:
            # 先按城市，再按省份
            rows = conn.execute(
                "SELECT * FROM policy_match_schools "
                "WHERE school_type = ? AND (city = ? OR province = ?) AND effective_year = 2026 "
                "ORDER BY priority ASC",
                (match_type, city, province),
            ).fetchall()
    finally:
        conn.close()

    matches = []
    for idx, r in enumerate(rows):
        d = dict(r)
        d["is_recommended"] = idx < 3  # 前 3 所标"推荐"
        matches.append(d)

    return {
        "stage": stage,
        "stage_label": stage_label,
        "match_type": match_type,
        "match_type_label": match_type_label,
        "city": city,
        "province": province,
        "matches": matches,
    }


def _resolve_match_target(grade: str):
    """根据 grade 字段判断学段和匹配类型

    Args:
        grade: v3.5.2 注册字段，使用以下 token 之一：
            PRIMARY_1~6, JUNIOR_1~3, SENIOR_1~3, UNIV_1~4, GRADUATED
            兼容：旧"2025入学"等入学年份格式（已弃用）

    Returns:
        (stage, stage_label, match_type, match_type_label)
    """
    s = (grade or "").strip().upper()
    if not s:
        return ("unknown", "未填写年级", None, "")

    # v3.5.2 统一 token 体系
    if s.startswith("PRIMARY_"):
        try:
            n = int(s.split("_")[1])
            return ("primary", f"小学（{n} 年级）", "tech_talent_junior", "科技特长生中学")
        except (ValueError, IndexError):
            return ("primary", "小学（年级未识别）", "tech_talent_junior", "科技特长生中学")
    elif s.startswith("JUNIOR_"):
        try:
            n = int(s.split("_")[1])
            label = ["", "初一", "初二", "初三"][n] if 1 <= n <= 3 else f"初{n}"
            return ("junior", f"初中（{label}）", "self_enroll_senior", "自招高中")
        except (ValueError, IndexError):
            return ("junior", "初中（年级未识别）", "self_enroll_senior", "自招高中")
    elif s.startswith("SENIOR_"):
        try:
            n = int(s.split("_")[1])
            label = ["", "高一", "高二", "高三"][n] if 1 <= n <= 3 else f"高{n}"
            return ("senior", f"高中（{label}）", "qiangji_university", "强基大学")
        except (ValueError, IndexError):
            return ("senior", "高中（年级未识别）", "qiangji_university", "强基大学")
    elif s.startswith("UNIV_"):
        try:
            n = int(s.split("_")[1])
            return ("college", f"大学（{['大一','大二','大三','大四'][n-1] if 1<=n<=4 else f'大{n}'}）", None, "已毕业")
        except (ValueError, IndexError):
            return ("college", "大学（年级未识别）", None, "已毕业")
    elif s == "GRADUATED":
        return ("graduated", "已毕业", None, "已毕业")

    # 兼容旧的"2025入学"等入学年份格式（v3.5.2 之前）
    import re as _re
    m = _re.search(r"(\d{4})", s)
    if m:
        year = int(m.group(1))
        current_year = 2026
        # 2025 入学 = 当时是 1 年级 → 2026 是 1 / 7 / 10（小学/初中/高中各取 1 套）
        # 这里用第一套（小学）做兜底
        grade_num = current_year - year + 1
        if 1 <= grade_num <= 6:
            return ("primary", f"小学（{grade_num} 年级）", "tech_talent_junior", "科技特长生中学")
        elif 7 <= grade_num <= 9:
            return ("junior", f"初中（{grade_num-6} 年级）", "self_enroll_senior", "自招高中")
        elif 10 <= grade_num <= 12:
            return ("senior", f"高中（{grade_num-9} 年级）", "qiangji_university", "强基大学")
        else:
            return ("college", f"已毕业（{year} 入学）", None, "已毕业")

    return ("unknown", "未识别学段", None, "")


def _city_to_province_for_match(city: str) -> str:
    """城市 → 省份（用于政策匹配降级查询）"""
    if not city:
        return ""
    direct = {"北京": "北京", "上海": "上海", "天津": "天津", "重庆": "重庆"}
    if city in direct:
        return direct[city]
    # 简化映射：部分常见城市
    mapping = {
        "杭州": "浙江", "宁波": "浙江", "温州": "浙江", "嘉兴": "浙江",
        "南京": "江苏", "苏州": "江苏", "无锡": "江苏", "常州": "江苏",
        "广州": "广东", "深圳": "广东", "东莞": "广东", "佛山": "广东",
        "成都": "四川", "绵阳": "四川", "重庆": "重庆",
        "武汉": "湖北", "长沙": "湖南", "郑州": "河南", "西安": "陕西",
        "青岛": "山东", "济南": "山东",
        "厦门": "福建", "福州": "福建",
        "合肥": "安徽", "南昌": "江西",
        "沈阳": "辽宁", "大连": "辽宁",
        "哈尔滨": "黑龙江", "长春": "吉林",
        "昆明": "云南", "贵阳": "贵州", "南宁": "广西", "海口": "海南",
        "兰州": "甘肃", "西宁": "青海", "银川": "宁夏",
        "乌鲁木齐": "新疆", "拉萨": "西藏", "呼和浩特": "内蒙古",
        "香港": "香港", "澳门": "澳门", "台北": "台湾",
    }
    return mapping.get(city, "")


def list_columns() -> list[str]:
    """返回 tasks 表当前的真实列名（供调试 / 健康检查）"""
    conn = _get_conn()
    rows = conn.execute("PRAGMA table_info(tasks)").fetchall()
    conn.close()
    return [row["name"] for row in rows]


def get_latest_done_task_for_uid(luogu_uid: str, since_hours: int = 24) -> dict | None:
    """v3.8 · 查最近 N 小时内该 UID 是否已生成过报告（用于每日 1 次限流）

    Args:
        luogu_uid: 洛谷 UID（字符串）
        since_hours: 限定 N 小时内（默认 24）

    Returns:
        若存在已完成的 report.md 任务，返回该任务字典；
        否则返回 None。

    判定条件：
      - tasks.luogu_uid = ?
      - tasks.status IN ('done', 'partial')
      - tasks.created_at >= now - N hours
      - 优先返回最近一条
    """
    uid = str(luogu_uid or "").strip()
    if not uid:
        return None
    conn = _get_conn()
    try:
        # 兼容老库（luogu_uid 列可能不存在）
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
        if "luogu_uid" not in cols:
            return None
        threshold = (datetime.now() - timedelta(hours=int(since_hours))).strftime("%Y-%m-%d %H:%M:%S")
        row = conn.execute(
            """
            SELECT t.task_id, t.status, t.created_at, t.html, t.student_name
            FROM tasks t
            WHERE t.luogu_uid = ?
              AND t.status IN ('done', 'partial')
              AND (t.created_at IS NULL OR t.created_at >= ?)
            ORDER BY t.created_at DESC
            LIMIT 1
            """,
            (uid, threshold),
        ).fetchone()
        if not row:
            return None
        return dict(row)
    finally:
        conn.close()


def insert_task(task_id: str, status: str = "queued", message: str = "排队中...", luogu_uid: str = ""):
    conn = _get_conn()
    try:
        # v3.8 · 幂等添加 luogu_uid 列（用于每日 1 次生成限制）
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN luogu_uid TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # 列已存在
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_luogu_uid ON tasks(luogu_uid)")
        except sqlite3.OperationalError:
            pass
        conn.execute(
            "INSERT OR IGNORE INTO tasks (task_id, status, message, created_at, luogu_uid) VALUES (?, ?, ?, ?, ?)",
            (task_id, status, message, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), str(luogu_uid or "").strip()),
        )
        conn.commit()
    finally:
        conn.close()


def delete_task(task_id: str) -> bool:
    """v3.8 · 物理删除一条任务（同时返回 report 文件路径，便于调用方清理磁盘）

    Returns:
        bool: True=删除成功；False=任务不存在
    """
    task_id = str(task_id or "").strip()
    if not task_id:
        return False
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT task_id, html, pdf, md FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def update_task(task_id: str, **kwargs):
    """更新任务字段，支持 status, message, html, pdf, md 等"""
    conn = _get_conn()
    fields = []
    values = []
    for k, v in kwargs.items():
        fields.append(f"{k} = ?")
        values.append(v)
    if not fields:
        conn.close()
        return
    values.append(task_id)
    sql = f"UPDATE tasks SET {', '.join(fields)} WHERE task_id = ?"
    conn.execute(sql, values)
    conn.commit()
    conn.close()


def get_task(task_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    return dict(row)


def list_tasks() -> list[dict]:
    """列出所有任务，按时间倒序"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM tasks ORDER BY eval_time DESC, created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    """获取统计数字"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    conn = _get_conn()
    total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    today = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE eval_time LIKE ? OR created_at LIKE ?",
        (f"{today_str}%", f"{today_str}%"),
    ).fetchone()[0]
    running = conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'running'").fetchone()[0]
    error = conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'error'").fetchone()[0]
    conn.close()
    return {
        "total": total,
        "today": today,
        "running": running,
        "error": error,
    }


# 初始化
init_db()


# -- smoke test：验证 v3.5 schema 完整 --
if __name__ == "__main__":
    EXPECTED_TABLES = {
        # v0
        "tasks",
        # v2 学员档案
        "students",
        "student_cookies",
        # v3.5 赛事 4 表
        "competitions",
        "student_competitions",
        "gesp_exams",
        "policy_events",
        # v3.5 业务 4 表
        "guardians",
        "weekly_reports",
        "student_goals",
        "activation_codes",
        "csp_awards",
    }
    EXPECTED_TASKS_COLS = {
        # 核心 + v1 → v2
        "task_id", "status", "message", "html", "pdf", "md",
        "student_name", "school", "grade", "solved_count", "failed_count",
        "eval_time", "stage", "source_code_success", "source_code_total",
        "ai_progress", "ai_elapsed_seconds", "retry_form_json", "created_at",
        # v2 → v3 学员关联
        "student_id",
    }
    EXPECTED_STUDENTS_COLS = {
        # v2 基础
        "id", "luogu_uid", "real_name", "school", "grade", "is_minor",
        "guardian_consent_at", "note", "created_at",
        # v3.5 GESP 6 字段
        "gesp_highest_passed", "gesp_latest_score",
        "gesp_can_exempt_csp_j", "gesp_can_exempt_csp_s",
        "gesp_exemption_expiry", "gesp_next_eligible_level",
    }

    conn = _get_conn()
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    actual_tables = {r["name"] for r in cur.fetchall()}
    missing_tables = EXPECTED_TABLES - actual_tables
    assert not missing_tables, f"缺表: {missing_tables} | 实际: {actual_tables}"

    tasks_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert EXPECTED_TASKS_COLS <= tasks_cols, (
        f"tasks 缺列: {EXPECTED_TASKS_COLS - tasks_cols}"
    )

    students_cols = {r["name"] for r in conn.execute("PRAGMA table_info(students)").fetchall()}
    assert EXPECTED_STUDENTS_COLS <= students_cols, (
        f"students 缺列: {EXPECTED_STUDENTS_COLS - students_cols}"
    )

    conn.close()
    print(f"[OK] task_store v3.5 schema smoke test")
    print(f"     tables: {len(actual_tables)} (>= {len(EXPECTED_TABLES)} expected)")
    print(f"     tasks cols: {len(tasks_cols)}")
    print(f"     students cols: {len(students_cols)}")

