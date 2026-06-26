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
    # v3.10.0 · 改为 foreign_keys=OFF:旧库 v3.9 迁移到 v3.10.0 时,students 表被 RENAME 又 DROP,
    #             依赖表(student_vjudge_data 等)的 FK 可能还指向已不存在的
    #             students__v3p9_backup,导致 "no such table" 错误。SQLite 没有
    #             ALTER TABLE ... ALTER FK,只能重建表才能修。本次只关 FK 校验,
    #             业务逻辑不受影响(查询都是显式 JOIN/IN)。
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = OFF")
    except Exception:
        # WAL 在某些只读 FS / 网络盘下不可用,失败时降级回默认（不致命）
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
    # v3.10.0 · 邮箱注册改造:
    #   - luogu_uid: 从 NOT NULL 改可空(老学员保留,新学员不填)
    #   - email: 唯一登录账号
    #   - short_id: 8 位随机 ID,用于 /me/<x> URL 隐藏邮箱
    #   - password_hash: BCrypt 哈希
    conn.execute("""
        CREATE TABLE IF NOT EXISTS students (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            luogu_uid       TEXT UNIQUE,                  -- v3.10.0 · 可空(已废弃)
            email           TEXT UNIQUE,                  -- v3.10.0 · 新主键(登录账号)
            short_id        TEXT UNIQUE,                  -- v3.10.0 · 8 位短 ID(URL 用)
            password_hash   TEXT,                         -- v3.10.0 · BCrypt 哈希
            real_name       TEXT,
            school          TEXT,
            grade           TEXT,
            is_minor        BOOLEAN NOT NULL DEFAULT 0,
            guardian_consent_at  DATETIME,
            note            TEXT,
            city            TEXT,
            province        TEXT,
            gender          TEXT,
            birth_date      TEXT,
            registered_via  TEXT DEFAULT 'admin',
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
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

    # ---- 5. v3.10.0 · 邮箱注册改造 ----
    #   老 students 表有 luogu_uid NOT NULL,新学员不再填 → 把 NOT NULL 去掉
    #   已有学员补 short_id(email 用 placeholder),luogu_uid 保留供迁移期引用
    _migrate_students_v3100(conn)

    conn.commit()
    conn.close()


def _migrate_students_v3100(conn: sqlite3.Connection) -> None:
    """v3.10.0 · students 表邮箱化迁移

    1. luogu_uid NOT NULL → 可空(老数据保留)
    2. 缺 email / short_id / password_hash / city / province / gender /
       birth_date / registered_via 列则补(防御老 schema)
    3. 给所有已存在的学员分配 short_id(8 位),email 置为
       legacy-<short_id>@noemail.local(标记无可用邮箱)
    """
    import secrets
    import string

    # 0) 防御:上次迁移失败可能残留 students__v3p9_backup 备份表,先清掉
    try:
        conn.execute("DROP TABLE IF EXISTS students__v3p9_backup")
    except Exception:
        pass

    # 1) 检查老 schema 是否是 NOT NULL
    cols = conn.execute("PRAGMA table_info(students)").fetchall()
    col_names = {c[1]: c for c in cols}
    has_luogu_notnull = False
    for c in cols:
        if c[1] == "luogu_uid" and c[3] == 1:  # notnull=1
            has_luogu_notnull = True
            break

    if has_luogu_notnull:
        # SQLite 不支持直接改 NOT NULL,要重建表
        # v3.10.0 · 关键修复:必须在 foreign_keys=ON 状态下 RENAME,这样依赖表(student_vjudge_data
        # 等)的 FK 引用会自动从 students__v3p9_backup 更新到新 students 表。否则 DROP 备份表后,
        # 任何对依赖表的 DELETE/INSERT/UPDATE 都会报"no such table: main.students__v3p9_backup"。
        print("[v3.10.0] students.luogu_uid 是 NOT NULL,执行表重建…")
        # 先确保 foreign_keys=ON(让 RENAME 自动更新依赖表的 FK)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("ALTER TABLE students RENAME TO students__v3p9_backup")
        # 新表(无 NOT NULL + 新字段)
        conn.execute("""
            CREATE TABLE students (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                luogu_uid       TEXT UNIQUE,
                email           TEXT UNIQUE,
                short_id        TEXT UNIQUE,
                password_hash   TEXT,
                real_name       TEXT,
                school          TEXT,
                grade           TEXT,
                is_minor        BOOLEAN NOT NULL DEFAULT 0,
                guardian_consent_at  DATETIME,
                note            TEXT,
                city            TEXT,
                province        TEXT,
                gender          TEXT,
                birth_date      TEXT,
                registered_via  TEXT DEFAULT 'admin',
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # 复制老数据(补默认值)
        conn.execute("""
            INSERT INTO students
              (id, luogu_uid, real_name, school, grade, is_minor,
               guardian_consent_at, note, city, province, gender,
               birth_date, registered_via, created_at)
            SELECT id, luogu_uid, real_name, school, grade, is_minor,
                   guardian_consent_at, note,
                   COALESCE(city, ''), COALESCE(province, ''), COALESCE(gender, ''),
                   COALESCE(birth_date, ''), COALESCE(registered_via, 'legacy'),
                   COALESCE(created_at, CURRENT_TIMESTAMP)
            FROM students__v3p9_backup
        """)
        conn.execute("DROP TABLE students__v3p9_backup")
        print("[v3.10.0] students 表重建完成")

    # 2) 防御式 ADD COLUMN(如果新表已用新 schema,这里都是 no-op)
    for col, decl in [
        ("email",          "TEXT UNIQUE"),
        ("short_id",       "TEXT UNIQUE"),
        ("password_hash",  "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE students ADD COLUMN {col} {decl}")
        except Exception:
            pass  # 已存在,忽略

    # 3) 给缺 short_id / email 的学员补 short_id
    rows = conn.execute("SELECT id FROM students WHERE short_id IS NULL OR short_id = ''").fetchall()
    if rows:
        alphabet = string.ascii_lowercase + string.digits
        existing = {r[0] for r in conn.execute("SELECT short_id FROM students WHERE short_id IS NOT NULL").fetchall()}
        for (sid,) in rows:
            # 8 位,排除易混淆的 0/o/1/l
            pool = "abcdefghijkmnpqrstuvwxyz23456789"
            for _ in range(20):
                short_id = "".join(secrets.choice(pool) for _ in range(8))
                if short_id not in existing:
                    break
            else:
                short_id = "u" + secrets.token_hex(3)  # 兜底
            email = f"legacy-{short_id}@noemail.local"
            conn.execute(
                "UPDATE students SET short_id = ?, email = ? WHERE id = ?",
                (short_id, email, sid),
            )
            existing.add(short_id)
        print(f"[v3.10.0] backfilled short_id/email for {len(rows)} legacy students")


# ============================================================
# v3.9.73 · AtCoder 跨平台数据（5 张表 + students.atcoder_handle）
# 原则: luogu_uid 永远主键,AtCoder 是附加属性
# 任何环节失败都不阻塞洛谷主报告生成
# ============================================================

def init_atcoder_tables() -> None:
    """v3.9.73 · 幂等创建 AtCoder 跨平台数据 5 张表。

    升级路径:
      v3.9.72 → v3.9.73: students 加 atcoder_handle 列 +
        student_atcoder_data + student_atcoder_ac_problems +
        student_atcoder_recent_subs + atcoder_fetch_tasks

    全部用 IF NOT EXISTS,反复执行不报错。
    """
    conn = _get_conn()
    try:
        # ---- 1. students.atcoder_handle 软引用(可空) ----
        # v3.9.73 · 轻量级引用,1:1
        # 留空 = 未绑,非空 = 已绑(唯一约束)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(students)")}
        if "atcoder_handle" not in cols:
            conn.execute("ALTER TABLE students ADD COLUMN atcoder_handle TEXT DEFAULT ''")
        # 唯一索引(忽略空串,允许多个学生都不绑)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_students_atcoder_handle
                ON students(atcoder_handle) WHERE atcoder_handle != ''
        """)

        # ---- 2. AtCoder 抓取快照(1:1 with student) ----
        conn.execute("""
            CREATE TABLE IF NOT EXISTS student_atcoder_data (
                student_id           INTEGER PRIMARY KEY REFERENCES students(id),
                handle               TEXT NOT NULL,
                rating               INTEGER DEFAULT 0,
                highest_rating       INTEGER DEFAULT 0,
                rank                 TEXT DEFAULT '',
                contests_count       INTEGER DEFAULT 0,
                ac_problems_count    INTEGER DEFAULT 0,
                hard_ac_count        INTEGER DEFAULT 0,
                recent_contest_rate  INTEGER DEFAULT 0,
                first_event_at       DATETIME,
                last_event_at        DATETIME,
                last_fetched_at      DATETIME,
                fetch_status         TEXT DEFAULT 'pending',
                fetch_error          TEXT DEFAULT '',
                raw_html_cache_path  TEXT DEFAULT '',
                link_status          TEXT DEFAULT 'pending',
                updated_at           DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ---- 3. AC 难题清单(1:N with student) ----
        conn.execute("""
            CREATE TABLE IF NOT EXISTS student_atcoder_ac_problems (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id   INTEGER NOT NULL REFERENCES students(id),
                contest_id   TEXT NOT NULL,
                problem_id   TEXT NOT NULL,
                title        TEXT DEFAULT '',
                difficulty   INTEGER DEFAULT 0,
                language     TEXT DEFAULT '',
                solved_at    DATETIME,
                UNIQUE(student_id, contest_id, problem_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ac_problems_student
                ON student_atcoder_ac_problems(student_id)
        """)

        # ---- 4. 最近提交(1:N,限 20 条/学生,用于代码 review) ----
        conn.execute("""
            CREATE TABLE IF NOT EXISTS student_atcoder_recent_subs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id      INTEGER NOT NULL REFERENCES students(id),
                contest_id      TEXT NOT NULL,
                problem_id      TEXT NOT NULL,
                result          TEXT NOT NULL,
                language        TEXT DEFAULT '',
                submit_time     DATETIME,
                source_url      TEXT DEFAULT '',
                UNIQUE(student_id, contest_id, problem_id, submit_time)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ac_recent_subs_student
                ON student_atcoder_recent_subs(student_id, submit_time DESC)
        """)

        # ---- 5. AtCoder 抓取异步任务跟踪(与 tasks 表同模式) ----
        conn.execute("""
            CREATE TABLE IF NOT EXISTS atcoder_fetch_tasks (
                task_id      TEXT PRIMARY KEY,
                student_id   INTEGER NOT NULL REFERENCES students(id),
                handle       TEXT NOT NULL,
                status       TEXT DEFAULT 'pending',
                trigger      TEXT DEFAULT 'user_link',
                retry_count  INTEGER DEFAULT 0,
                error_msg    TEXT DEFAULT '',
                started_at   DATETIME,
                finished_at  DATETIME,
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ac_tasks_student
                ON atcoder_fetch_tasks(student_id, created_at DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ac_tasks_status
                ON atcoder_fetch_tasks(status, started_at)
        """)

        conn.commit()
    finally:
        conn.close()


def recover_orphan_atcoder_tasks() -> int:
    """v3.9.73 · 容器启动时清理残留: 状态为 pending/fetching 的任务
    视为孤儿(进程崩溃或容器重启),置为 failed,等下次手动触发重抓。

    返回清理条数。"""
    conn = _get_conn()
    try:
        cur = conn.execute("""
            UPDATE atcoder_fetch_tasks
            SET status='failed',
                error_msg='容器重启,任务丢弃,等下次手动刷新',
                finished_at=CURRENT_TIMESTAMP
            WHERE status IN ('pending', 'fetching')
        """)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def atcoder_link_handle(luogu_uid: str, handle: str) -> dict:
    """v3.9.73 · 绑 AtCoder handle。

    v3.10.0 · 参数可传 luogu_uid 或 short_id。

    成功: {'ok': True, 'student_id': int, 'handle': str}
    失败: {'ok': False, 'error': str, 'code': 'invalid_format'|'not_found'|'already_bound'}
    """
    import re
    if not re.match(r"^[a-zA-Z0-9_]{3,20}$", handle):
        return {"ok": False, "error": "handle 格式不对,3-20 位字母数字下划线", "code": "invalid_format"}

    conn = _get_conn()
    try:
        # 查 student
        row = conn.execute(
            "SELECT id, atcoder_handle FROM students WHERE short_id=? OR luogu_uid=? LIMIT 1",
            (luogu_uid, luogu_uid),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"洛谷 UID {luogu_uid} 不存在", "code": "luogu_uid_not_found"}

        student_id = row["id"]
        # UNIQUE 冲突: handle 已被他人绑
        dup = conn.execute(
            "SELECT id, luogu_uid FROM students WHERE atcoder_handle=? AND id != ?",
            (handle, student_id),
        ).fetchone()
        if dup:
            return {
                "ok": False,
                "error": f"该 handle 已被其他学员(UID={dup['luogu_uid']})绑定",
                "code": "already_bound",
            }

        # 写 handle(覆盖旧值)
        conn.execute(
            "UPDATE students SET atcoder_handle=? WHERE id=?", (handle, student_id)
        )
        # 清旧 AtCoder 数据(改绑场景)
        conn.execute("DELETE FROM student_atcoder_data WHERE student_id=?", (student_id,))
        conn.execute("DELETE FROM student_atcoder_ac_problems WHERE student_id=?", (student_id,))
        conn.execute("DELETE FROM student_atcoder_recent_subs WHERE student_id=?", (student_id,))
        # 插抓取任务
        import time
        task_id = f"AC-{student_id}-{int(time.time() * 1000)}"
        conn.execute(
            """INSERT INTO atcoder_fetch_tasks
               (task_id, student_id, handle, status, trigger, started_at)
               VALUES (?, ?, ?, 'pending', 'user_link', CURRENT_TIMESTAMP)""",
            (task_id, student_id, handle),
        )
        conn.commit()
        return {"ok": True, "student_id": student_id, "handle": handle, "task_id": task_id}
    except Exception as e:
        return {"ok": False, "error": str(e), "code": "db_error"}
    finally:
        conn.close()


def atcoder_pickup_pending_task() -> Optional[dict]:
    """v3.9.73 · 工作线程调用: 取一个 pending 任务,标 fetching,返回。

    使用 IMMEDIATE 事务避免两个 worker 抢同一任务。
    返回 None 表示没有待抓任务。"""
    conn = _get_conn()
    try:
        # 先抢: UPDATE ... WHERE status='pending' RETURNING
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""
            SELECT task_id, student_id, handle FROM atcoder_fetch_tasks
            WHERE status='pending'
            ORDER BY created_at ASC
            LIMIT 1
        """).fetchone()
        if not row:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE atcoder_fetch_tasks SET status='fetching', started_at=CURRENT_TIMESTAMP WHERE task_id=?",
            (row["task_id"],),
        )
        conn.execute("COMMIT")
        return {"task_id": row["task_id"], "student_id": row["student_id"], "handle": row["handle"]}
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return None
    finally:
        conn.close()


def atcoder_finish_task(task_id: str, status: str, error_msg: str = "") -> None:
    """v3.9.73 · 工作线程调用: 标记任务完成(ok / failed / rate_limited)。"""
    conn = _get_conn()
    try:
        conn.execute(
            """UPDATE atcoder_fetch_tasks
               SET status=?, error_msg=?, finished_at=CURRENT_TIMESTAMP
               WHERE task_id=?""",
            (status, error_msg, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def atcoder_persist_data(student_id: int, handle: str, raw: dict) -> None:
    """v3.9.73 · 工作线程调用: 把抓取结果落库(student_atcoder_data + ac_problems + recent_subs)。

    raw 字段约定(由 atcoder_fetcher.parse_* 返回):
        handle, rating, highest_rating, rank, contests_count,
        ac_problems_count, hard_ac_count, recent_contest_rate,
        first_event_at, last_event_at, ac_problems[], recent_subs[],
        fetch_status, fetch_error, raw_html_cache_path
    """
    conn = _get_conn()
    try:
        # 主表 UPSERT
        conn.execute("""
            INSERT INTO student_atcoder_data (
                student_id, handle, rating, highest_rating, rank,
                contests_count, ac_problems_count, hard_ac_count,
                recent_contest_rate, first_event_at, last_event_at,
                last_fetched_at, fetch_status, fetch_error,
                raw_html_cache_path, link_status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, 'ok', CURRENT_TIMESTAMP)
            ON CONFLICT(student_id) DO UPDATE SET
                handle=excluded.handle,
                rating=excluded.rating,
                highest_rating=excluded.highest_rating,
                rank=excluded.rank,
                contests_count=excluded.contests_count,
                ac_problems_count=excluded.ac_problems_count,
                hard_ac_count=excluded.hard_ac_count,
                recent_contest_rate=excluded.recent_contest_rate,
                first_event_at=excluded.first_event_at,
                last_event_at=excluded.last_event_at,
                last_fetched_at=CURRENT_TIMESTAMP,
                fetch_status=excluded.fetch_status,
                fetch_error=excluded.fetch_error,
                raw_html_cache_path=excluded.raw_html_cache_path,
                link_status='ok',
                updated_at=CURRENT_TIMESTAMP
        """, (
            student_id, handle,
            raw.get("rating", 0), raw.get("highest_rating", 0), raw.get("rank", ""),
            raw.get("contests_count", 0), raw.get("ac_problems_count", 0),
            raw.get("hard_ac_count", 0), raw.get("recent_contest_rate", 0),
            raw.get("first_event_at"), raw.get("last_event_at"),
            raw.get("fetch_status", "ok"), raw.get("fetch_error", ""),
            raw.get("raw_html_cache_path", ""),
        ))

        # AC 难题: 清旧写新
        conn.execute("DELETE FROM student_atcoder_ac_problems WHERE student_id=?", (student_id,))
        for p in raw.get("ac_problems", [])[:200]:  # 限 200 条
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO student_atcoder_ac_problems
                    (student_id, contest_id, problem_id, title, difficulty, language, solved_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    student_id,
                    p.get("contest_id", ""),
                    p.get("problem_id", ""),
                    p.get("title", ""),
                    int(p.get("difficulty", 0) or 0),
                    p.get("language", ""),
                    p.get("solved_at"),
                ))
            except Exception:
                pass

        # 最近提交: 清旧写新
        conn.execute("DELETE FROM student_atcoder_recent_subs WHERE student_id=?", (student_id,))
        for s in raw.get("recent_subs", [])[:20]:  # 限 20 条
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO student_atcoder_recent_subs
                    (student_id, contest_id, problem_id, result, language, submit_time, source_url)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    student_id,
                    s.get("contest_id", ""),
                    s.get("problem_id", ""),
                    s.get("result", ""),
                    s.get("language", ""),
                    s.get("submit_time"),
                    s.get("source_url", ""),
                ))
            except Exception:
                pass

        conn.commit()
    finally:
        conn.close()


def atcoder_mark_failed(student_id: int, error_msg: str, status: str = "failed") -> None:
    """v3.9.73 · 工作线程调用: 抓取失败时写状态(不删旧数据,留个底)。"""
    conn = _get_conn()
    try:
        conn.execute("""
            INSERT INTO student_atcoder_data (
                student_id, handle, link_status, fetch_status, fetch_error, updated_at
            ) VALUES (?, '', ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(student_id) DO UPDATE SET
                link_status=excluded.link_status,
                fetch_status=excluded.fetch_status,
                fetch_error=excluded.fetch_error,
                updated_at=CURRENT_TIMESTAMP
        """, (student_id, status, status, error_msg))
        conn.commit()
    finally:
        conn.close()


def get_atcoder_context(luogu_uid: str) -> dict:
    """v3.9.73 · 给报告 AI 用的只读接口。返回 dict,无数据时 link_status='unlinked'。

    v3.10.0 · 参数可传 luogu_uid 或 short_id。
    """
    empty = {
        "handle": "", "rating": 0, "highest_rating": 0, "rank": "", "rank_zh": "",
        "contests_count": 0, "ac_problems_count": 0, "hard_ac_count": 0,
        "recent_contest_rate": 0, "link_status": "unlinked",
        "last_fetched_at": "", "fetch_error": "",
        "ac_highlights": [], "recent_subs": [],
    }
    conn = _get_conn()
    try:
        srow = conn.execute(
            "SELECT id, atcoder_handle FROM students WHERE short_id=? OR luogu_uid=? LIMIT 1",
            (luogu_uid, luogu_uid),
        ).fetchone()
        if not srow or not srow["atcoder_handle"]:
            return empty

        sid = srow["id"]
        d = conn.execute(
            "SELECT * FROM student_atcoder_data WHERE student_id=?", (sid,)
        ).fetchone()
        if not d:
            return {**empty, "handle": srow["atcoder_handle"], "link_status": "pending"}

        # 派生: 中文段位名
        rank_zh_map = {
            "gray": "灰色", "brown": "茶色", "green": "绿色", "cyan": "青色",
            "blue": "蓝色", "yellow": "黄色", "orange": "橙色", "red": "红色",
        }
        # 7d 陈旧判断
        link_status = d["link_status"] or "ok"
        try:
            from datetime import datetime, timedelta
            last = datetime.fromisoformat(d["last_fetched_at"]) if d["last_fetched_at"] else None
            if link_status == "ok" and last and (datetime.now() - last) > timedelta(days=7):
                link_status = "stale"
        except Exception:
            pass

        # AC 高光(取 8 条,按难度倒序)
        highlights = [dict(r) for r in conn.execute("""
            SELECT contest_id, problem_id, title, difficulty, language
            FROM student_atcoder_ac_problems
            WHERE student_id=?
            ORDER BY difficulty DESC, solved_at DESC
            LIMIT 8
        """, (sid,)).fetchall()]

        # 最近提交(取 10 条)
        subs = [dict(r) for r in conn.execute("""
            SELECT contest_id, problem_id, result, language, source_url
            FROM student_atcoder_recent_subs
            WHERE student_id=?
            ORDER BY submit_time DESC
            LIMIT 10
        """, (sid,)).fetchall()]

        return {
            "handle": d["handle"] or srow["atcoder_handle"],
            "rating": d["rating"] or 0,
            "highest_rating": d["highest_rating"] or 0,
            "rank": d["rank"] or "",
            "rank_zh": rank_zh_map.get(d["rank"] or "", ""),
            "contests_count": d["contests_count"] or 0,
            "ac_problems_count": d["ac_problems_count"] or 0,
            "hard_ac_count": d["hard_ac_count"] or 0,
            "recent_contest_rate": d["recent_contest_rate"] or 0,
            "link_status": link_status,
            "last_fetched_at": d["last_fetched_at"] or "",
            "fetch_error": d["fetch_error"] or "",
            "ac_highlights": highlights,
            "recent_subs": subs,
        }
    finally:
        conn.close()


def atcoder_should_refresh(luogu_uid: str) -> bool:
    """v3.9.73 · 报告生成时判断: 是否需要触发后台刷新(陈旧/失败/限流)。"""
    ctx = get_atcoder_context(luogu_uid)
    return ctx["link_status"] in ("stale", "failed", "rate_limited", "pending")


def atcoder_enqueue_refresh(luogu_uid: str, trigger: str = "report_gen") -> Optional[str]:
    """v3.9.73 · 报告生成时调用: 若陈旧则入队一个刷新任务,返回 task_id;否则 None。

    不 await,不等结果,直接 return。

    v3.10.0 · 参数可传 luogu_uid 或 short_id。
    """
    conn = _get_conn()
    try:
        srow = conn.execute(
            "SELECT id, atcoder_handle FROM students WHERE short_id=? OR luogu_uid=? LIMIT 1",
            (luogu_uid, luogu_uid),
        ).fetchone()
        if not srow or not srow["atcoder_handle"]:
            return None
        # 节流: 1h 内已有进行中任务就不重复入队
        existing = conn.execute("""
            SELECT task_id FROM atcoder_fetch_tasks
            WHERE student_id=? AND status IN ('pending', 'fetching')
              AND created_at > datetime('now', '-1 hour')
        """, (srow["id"],)).fetchone()
        if existing:
            return None
        import time
        task_id = f"AC-R-{srow['id']}-{int(time.time() * 1000)}"
        conn.execute("""
            INSERT INTO atcoder_fetch_tasks
            (task_id, student_id, handle, status, trigger, started_at)
            VALUES (?, ?, ?, 'pending', ?, CURRENT_TIMESTAMP)
        """, (task_id, srow["id"], srow["atcoder_handle"], trigger))
        conn.commit()
        return task_id
    finally:
        conn.close()


# ============================================================
# v3.9.74 · VJudge 跨平台数据(取代 AtCoder,只抓公开数据)
# 设计原则:
#   - handle(username) 软引用(可空)+ 唯一约束
#   - 4 张表: 主页快照 + 已解决题 + 抓取任务 + OJ/标签分布
#   - 复用 atcoder 字段语义(students.<platform>_handle 是惯例)
# ============================================================

def init_vjudge_tables() -> None:
    """v3.9.74 · 幂等创建 VJudge 跨平台数据 4 张表。

    升级路径:
      v3.9.73 → v3.9.74: students 加 vjudge_username 列 +
        student_vjudge_data + student_vjudge_solved +
        student_vjudge_fetch_tasks + student_vjudge_oj_stats

    全部用 IF NOT EXISTS,反复执行不报错。
    """
    conn = _get_conn()
    try:
        # ---- 1. students.vjudge_username 软引用(可空) ----
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(students)")}
        if "vjudge_username" not in cols:
            conn.execute("ALTER TABLE students ADD COLUMN vjudge_username TEXT DEFAULT ''")
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_students_vjudge_username
                ON students(vjudge_username) WHERE vjudge_username != ''
        """)

        # ---- 2. VJudge 主页快照(1:1 with student) ----
        conn.execute("""
            CREATE TABLE IF NOT EXISTS student_vjudge_data (
                student_id           INTEGER PRIMARY KEY REFERENCES students(id),
                username             TEXT NOT NULL,
                nick                 TEXT DEFAULT '',
                total_submissions    INTEGER DEFAULT 0,
                total_ac             INTEGER DEFAULT 0,
                total_wa             INTEGER DEFAULT 0,
                total_tle            INTEGER DEFAULT 0,
                total_re             INTEGER DEFAULT 0,
                total_ce             INTEGER DEFAULT 0,
                ac_rate              REAL DEFAULT 0.0,
                solved_count         INTEGER DEFAULT 0,
                register_time        DATETIME,
                last_fetched_at      DATETIME,
                fetch_status         TEXT DEFAULT 'pending',
                fetch_error          TEXT DEFAULT '',
                link_status          TEXT DEFAULT 'pending',
                updated_at           DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ---- 3. 已解决题列表(1:N with student,单次最多 200 条) ----
        conn.execute("""
            CREATE TABLE IF NOT EXISTS student_vjudge_solved (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id   INTEGER NOT NULL REFERENCES students(id),
                oj_source    TEXT NOT NULL,
                problem_id   TEXT NOT NULL,
                title        TEXT DEFAULT '',
                ac_time      DATETIME,
                UNIQUE(student_id, oj_source, problem_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vj_solved_student
                ON student_vjudge_solved(student_id, ac_time DESC)
        """)

        # ---- 4. VJudge 抓取任务队列 ----
        conn.execute("""
            CREATE TABLE IF NOT EXISTS student_vjudge_fetch_tasks (
                task_id      TEXT PRIMARY KEY,
                student_id   INTEGER NOT NULL REFERENCES students(id),
                username     TEXT NOT NULL,
                status       TEXT DEFAULT 'pending',
                trigger      TEXT DEFAULT 'user_link',
                retry_count  INTEGER DEFAULT 0,
                error_msg    TEXT DEFAULT '',
                started_at   DATETIME,
                finished_at  DATETIME,
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # v3.10.0.4 · 进度信息列(迁移:已有表加列)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(student_vjudge_fetch_tasks)").fetchall()]
        if "progress_msg" not in cols:
            conn.execute("ALTER TABLE student_vjudge_fetch_tasks ADD COLUMN progress_msg TEXT DEFAULT ''")
        if "progress_step" not in cols:
            conn.execute("ALTER TABLE student_vjudge_fetch_tasks ADD COLUMN progress_step INTEGER DEFAULT 0")
        if "progress_total" not in cols:
            conn.execute("ALTER TABLE student_vjudge_fetch_tasks ADD COLUMN progress_total INTEGER DEFAULT 0")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vj_tasks_student
                ON student_vjudge_fetch_tasks(student_id, created_at DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vj_tasks_status
                ON student_vjudge_fetch_tasks(status, started_at)
        """)

        # ---- 5. 各 OJ 分布统计(派生,加速展示) ----
        conn.execute("""
            CREATE TABLE IF NOT EXISTS student_vjudge_oj_stats (
                student_id   INTEGER NOT NULL REFERENCES students(id),
                oj_source    TEXT NOT NULL,
                solved_count INTEGER DEFAULT 0,
                PRIMARY KEY (student_id, oj_source)
            )
        """)

        # ---- 6. v3.11 · 学员主动上传的历史代码(书签导出 / zip / JSON)
        # 数据来源:学员本机书签抓取,合规(用户主动提供)
        # 用途:AI 报告"代码维度"分析(算法/风格/复杂度/改进建议)
        # 与 student_vjudge_solved 区别:本表存的是完整源码,而非只 OJ+题号
        conn.execute("""
            CREATE TABLE IF NOT EXISTS student_imported_codes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id   INTEGER NOT NULL REFERENCES students(id),
                problem_id   TEXT DEFAULT '',
                title        TEXT DEFAULT '',
                lang         TEXT DEFAULT '',
                verdict      TEXT DEFAULT '',
                time_ms      INTEGER DEFAULT 0,
                memory_kb    INTEGER DEFAULT 0,
                source       TEXT DEFAULT '',   -- bookmarklet / json_upload / zip_upload / paste
                source_code  TEXT DEFAULT '',
                ac_time      DATETIME,
                imported_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(student_id, source, problem_id, ac_time)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_imp_student
                ON student_imported_codes(student_id, imported_at DESC)
        """)

        conn.commit()
    finally:
        conn.close()


def recover_orphan_vjudge_tasks() -> int:
    """v3.9.74 · 容器启动时清理残留的 VJudge 抓取任务(pending/fetching)。"""
    conn = _get_conn()
    try:
        cur = conn.execute("""
            UPDATE student_vjudge_fetch_tasks
            SET status='failed',
                error_msg='容器重启,任务丢弃,等下次手动刷新',
                finished_at=CURRENT_TIMESTAMP
            WHERE status IN ('pending', 'fetching')
        """)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def vjudge_link_username(luogu_uid: str, username: str) -> dict:
    """v3.9.74 · 绑 VJudge username。

    v3.10.0 · 参数 luogu_uid 可传 luogu_uid 或 short_id(优先用 short_id 查,找不到 fallback)

    返回 dict:
      ok: bool
      error: str(失败原因)
      already_bound: bool(该 username 已被其他学员绑定)
    """
    if not username or not username.strip():
        return {"ok": False, "error": "username 为空"}
    username = username.strip()
    # 简单合法性: VJudge username 允许字母/数字/_/-, 3-30 字符
    import re
    if not re.match(r"^[A-Za-z0-9_\-]{3,30}$", username):
        return {"ok": False, "error": "username 格式不合法(3-30 字母/数字/_/-)"}

    conn = _get_conn()
    try:
        # v3.10.0 · 兼容 short_id / luogu_uid
        srow = conn.execute(
            "SELECT id, luogu_uid, vjudge_username FROM students WHERE short_id=? OR luogu_uid=? LIMIT 1",
            (luogu_uid, luogu_uid),
        ).fetchone()
        if not srow:
            return {"ok": False, "error": f"学员 {luogu_uid} 不存在"}

        # 是否已被其他学员绑
        dup = conn.execute(
            "SELECT luogu_uid FROM students WHERE vjudge_username=? AND id != ?",
            (username, srow["id"])
        ).fetchone()
        if dup:
            return {"ok": False, "error": f"VJudge 用户 {username} 已被其他学员绑定",
                    "already_bound": True, "ok": False}

        # 写入
        conn.execute(
            "UPDATE students SET vjudge_username=? WHERE id=?", (username, srow["id"])
        )
        # 删除旧快照(如果有),强制重新抓
        conn.execute("DELETE FROM student_vjudge_data WHERE student_id=?", (srow["id"],))
        conn.execute("DELETE FROM student_vjudge_solved WHERE student_id=?", (srow["id"],))
        conn.execute("DELETE FROM student_vjudge_oj_stats WHERE student_id=?", (srow["id"],))
        conn.commit()
        return {"ok": True, "student_id": srow["id"], "username": username}
    finally:
        conn.close()


def vjudge_unlink(luogu_uid: str) -> bool:
    """v3.9.74 · 解绑 VJudge。返回是否成功。

    v3.10.0 · 参数可传 luogu_uid 或 short_id。
    """
    conn = _get_conn()
    try:
        srow = conn.execute(
            "SELECT id FROM students WHERE short_id=? OR luogu_uid=? LIMIT 1",
            (luogu_uid, luogu_uid),
        ).fetchone()
        if not srow:
            return False
        sid = srow["id"]
        conn.execute("UPDATE students SET vjudge_username='' WHERE id=?", (sid,))
        conn.execute("DELETE FROM student_vjudge_data WHERE student_id=?", (sid,))
        conn.execute("DELETE FROM student_vjudge_solved WHERE student_id=?", (sid,))
        conn.execute("DELETE FROM student_vjudge_oj_stats WHERE student_id=?", (sid,))
        # 未完成任务也清掉
        conn.execute("""
            DELETE FROM student_vjudge_fetch_tasks
            WHERE student_id=? AND status IN ('pending', 'fetching')
        """, (sid,))
        conn.commit()
        return True
    finally:
        conn.close()


def vjudge_enqueue_refresh(luogu_uid: str, trigger: str = "report_gen") -> Optional[str]:
    """v3.9.74 · 入队抓取任务(节流: 1h 内已有进行中任务则跳过)。

    v3.10.0 · 参数可传 luogu_uid 或 short_id。
    """
    conn = _get_conn()
    try:
        srow = conn.execute(
            "SELECT id, vjudge_username FROM students WHERE short_id=? OR luogu_uid=? LIMIT 1",
            (luogu_uid, luogu_uid),
        ).fetchone()
        if not srow or not srow["vjudge_username"]:
            return None
        sid = srow["id"]
        existing = conn.execute("""
            SELECT task_id FROM student_vjudge_fetch_tasks
            WHERE student_id=? AND status IN ('pending', 'fetching')
              AND created_at > datetime('now', '-1 hour')
            LIMIT 1
        """, (sid,)).fetchone()
        if existing:
            return None
        import time
        task_id = f"VJ-R-{sid}-{int(time.time() * 1000)}"
        # v3.10.0.13 · 入队即写 progress=1/100 + started_at, 避免前端轮询看到 0/0
        # (前 v3.10.0.4 把 progress 写到 0/3 但 template 渲染时 0/0 显示"准备中",
        #  30s 内 step 没动过就会弹"卡住"提示,其实 worker 还没 pickup)
        # 现在 progress_total=100, 完整生命周期: 1→10→15→20→45→65→85→95→98→100
        conn.execute("""
            INSERT INTO student_vjudge_fetch_tasks
            (task_id, student_id, username, status, trigger, started_at,
             progress_step, progress_total, progress_msg)
            VALUES (?, ?, ?, 'pending', ?, CURRENT_TIMESTAMP,
                    1, 100, '⏳ 已入队,等待 worker pickup…')
        """, (task_id, sid, srow["vjudge_username"], trigger))
        # v3.10.0.4 · 同步把 student_vjudge_data 标为 pending
        # 否则前端模板一直显示"已同步"卡片,看不到进度条
        conn.execute("""
            UPDATE student_vjudge_data
            SET link_status='pending', updated_at=CURRENT_TIMESTAMP
            WHERE student_id=?
        """, (sid,))
        conn.commit()
        return task_id
    finally:
        conn.close()


def vjudge_pickup_pending_task() -> Optional[dict]:
    """v3.9.74 · worker 拉取一个 pending 任务(原子,mark 为 fetching)。

    返回 dict 或 None:
      task_id, student_id, username
    """
    conn = _get_conn()
    try:
        # 先找最早 pending
        row = conn.execute("""
            SELECT task_id, student_id, username, trigger, retry_count
            FROM student_vjudge_fetch_tasks
            WHERE status='pending'
            ORDER BY created_at ASC
            LIMIT 1
        """).fetchone()
        if not row:
            return None
        # 原子抢锁
        # v3.10.0.13 · pickup 时把 step 推到 10/100, msg 改为"🔄 worker 已拾取"
        # (前 v3.10.0.4 写到 0/3 会让前端 30s 内看不到 step 推进)
        cur = conn.execute("""
            UPDATE student_vjudge_fetch_tasks
            SET status='fetching', started_at=CURRENT_TIMESTAMP,
                progress_step=10, progress_total=100,
                progress_msg='🔄 worker 已拾取,准备抓 vjudge.net…'
            WHERE task_id=? AND status='pending'
        """, (row["task_id"],))
        if cur.rowcount == 0:
            # 被别的 worker 抢了
            return None
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def vjudge_update_progress(task_id: str, step: int, total: int, msg: str) -> None:
    """v3.10.0.4 · 更新任务进度(worker 在每一步调用)。
    step/total 用于前端渲染进度条;msg 是中文描述。
    """
    conn = _get_conn()
    try:
        conn.execute("""
            UPDATE student_vjudge_fetch_tasks
            SET progress_step=?, progress_total=?, progress_msg=?
            WHERE task_id=?
        """, (int(step), int(total), str(msg or ""), task_id))
        conn.commit()
    finally:
        conn.close()


def vjudge_finish_task(task_id: str, status: str, error_msg: str = "") -> None:
    """v3.9.74 · 标记任务完成(succeeded/failed/rate_limited)。"""
    conn = _get_conn()
    try:
        conn.execute("""
            UPDATE student_vjudge_fetch_tasks
            SET status=?, error_msg=?, finished_at=CURRENT_TIMESTAMP,
                progress_msg=CASE WHEN ? IN ('succeeded') THEN '完成' ELSE progress_msg END,
                progress_step=CASE WHEN ? IN ('succeeded') THEN progress_total ELSE progress_step END
            WHERE task_id=?
        """, (status, error_msg, status, status, task_id))
        conn.commit()
    finally:
        conn.close()


def vjudge_persist_data(student_id: int, username: str, raw: dict) -> None:
    """v3.9.74 · 抓取成功 → 落库(覆盖旧数据)。"""
    conn = _get_conn()
    try:
        # 1. 主页快照(upsert)
        conn.execute("""
            INSERT INTO student_vjudge_data
            (student_id, username, nick, total_submissions, total_ac,
             total_wa, total_tle, total_re, total_ce, ac_rate,
             solved_count, register_time, last_fetched_at,
             fetch_status, fetch_error, link_status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP,
                    'succeeded', '', 'ok', CURRENT_TIMESTAMP)
            ON CONFLICT(student_id) DO UPDATE SET
              username=excluded.username,
              nick=excluded.nick,
              total_submissions=excluded.total_submissions,
              total_ac=excluded.total_ac,
              total_wa=excluded.total_wa,
              total_tle=excluded.total_tle,
              total_re=excluded.total_re,
              total_ce=excluded.total_ce,
              ac_rate=excluded.ac_rate,
              solved_count=excluded.solved_count,
              register_time=excluded.register_time,
              last_fetched_at=CURRENT_TIMESTAMP,
              fetch_status='succeeded',
              fetch_error='',
              link_status='ok',
              updated_at=CURRENT_TIMESTAMP
        """, (
            student_id, username,
            raw.get("nick", ""),
            int(raw.get("total_submissions", 0) or 0),
            int(raw.get("total_ac", 0) or 0),
            int(raw.get("total_wa", 0) or 0),
            int(raw.get("total_tle", 0) or 0),
            int(raw.get("total_re", 0) or 0),
            int(raw.get("total_ce", 0) or 0),
            float(raw.get("ac_rate", 0.0) or 0.0),
            int(raw.get("solved_count", 0) or 0),
            raw.get("register_time") or None,
        ))

        # 2. 已解决题列表(全量替换)
        conn.execute("DELETE FROM student_vjudge_solved WHERE student_id=?", (student_id,))
        solved = raw.get("solved_list") or []
        for p in solved:
            conn.execute("""
                INSERT OR IGNORE INTO student_vjudge_solved
                (student_id, oj_source, problem_id, title, ac_time)
                VALUES (?, ?, ?, ?, ?)
            """, (
                student_id,
                p.get("oj", ""),
                p.get("problem_id", ""),
                p.get("title", ""),
                p.get("ac_time") or None,
            ))

        # 3. OJ 分布统计
        conn.execute("DELETE FROM student_vjudge_oj_stats WHERE student_id=?", (student_id,))
        oj_stats = raw.get("oj_stats") or {}
        for oj, cnt in oj_stats.items():
            conn.execute("""
                INSERT INTO student_vjudge_oj_stats
                (student_id, oj_source, solved_count)
                VALUES (?, ?, ?)
            """, (student_id, oj, int(cnt)))

        conn.commit()
    finally:
        conn.close()


def vjudge_mark_failed(student_id: int, error_msg: str, status: str = "failed") -> None:
    """v3.9.74 · 抓取失败 → 更新 link_status。"""
    conn = _get_conn()
    try:
        # upsert 一条失败记录(避免学生数据全无)
        conn.execute("""
            INSERT INTO student_vjudge_data
            (student_id, username, fetch_status, fetch_error, link_status, updated_at)
            VALUES (?, '', ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(student_id) DO UPDATE SET
              fetch_status=excluded.fetch_status,
              fetch_error=excluded.fetch_error,
              link_status=excluded.link_status,
              updated_at=CURRENT_TIMESTAMP
        """, (student_id, status, error_msg, status))
        conn.commit()
    finally:
        conn.close()


def get_vjudge_context(luogu_uid: str) -> dict:
    """v3.9.74 · 给报告 AI / UI 用的只读接口。返回 dict,无数据时 link_status='unlinked'。

    v3.10.0 · 参数可传 luogu_uid 或 short_id。
    """
    empty = {
        "username": "", "nick": "",
        "total_submissions": 0, "total_ac": 0,
        "total_wa": 0, "total_tle": 0, "total_re": 0, "total_ce": 0,
        "ac_rate": 0.0, "solved_count": 0,
        "register_time": "", "last_fetched_at": "", "fetch_error": "",
        "link_status": "unlinked",
        "recent_solved": [],
        "oj_stats": [],
    }
    conn = _get_conn()
    try:
        srow = conn.execute(
            "SELECT id, vjudge_username FROM students WHERE short_id=? OR luogu_uid=? LIMIT 1",
            (luogu_uid, luogu_uid),
        ).fetchone()
        if not srow or not srow["vjudge_username"]:
            return empty

        sid = srow["id"]
        d = conn.execute(
            "SELECT * FROM student_vjudge_data WHERE student_id=?", (sid,)
        ).fetchone()
        if not d:
            return {**empty, "username": srow["vjudge_username"], "link_status": "pending"}

        # 7d 陈旧判断
        link_status = d["link_status"] or "ok"
        if link_status == "ok" and d["last_fetched_at"]:
            try:
                from datetime import datetime, timedelta
                fetched_dt = datetime.fromisoformat(str(d["last_fetched_at"]).replace(" ", "T"))
                if datetime.now() - fetched_dt > timedelta(days=7):
                    link_status = "stale"
            except Exception:
                pass

        # 最近 5 个已解决题
        recent = conn.execute("""
            SELECT oj_source, problem_id, title, ac_time
            FROM student_vjudge_solved
            WHERE student_id=?
            ORDER BY ac_time DESC
            LIMIT 5
        """, (sid,)).fetchall()
        recent_list = []
        for r in recent:
            recent_list.append({
                "oj": r["oj_source"],
                "problem_id": r["problem_id"],
                "title": r["title"],
                "ac_time": r["ac_time"],
            })

        # OJ 分布
        oj_rows = conn.execute("""
            SELECT oj_source, solved_count
            FROM student_vjudge_oj_stats
            WHERE student_id=? AND solved_count > 0
            ORDER BY solved_count DESC
            LIMIT 10
        """, (sid,)).fetchall()
        oj_list = [{"oj": r["oj_source"], "count": r["solved_count"]} for r in oj_rows]

        return {
            "username": srow["vjudge_username"],
            "nick": d["nick"] or "",
            "total_submissions": d["total_submissions"] or 0,
            "total_ac": d["total_ac"] or 0,
            "total_wa": d["total_wa"] or 0,
            "total_tle": d["total_tle"] or 0,
            "total_re": d["total_re"] or 0,
            "total_ce": d["total_ce"] or 0,
            "ac_rate": float(d["ac_rate"] or 0.0),
            "solved_count": d["solved_count"] or 0,
            "register_time": d["register_time"] or "",
            "last_fetched_at": d["last_fetched_at"] or "",
            "fetch_error": d["fetch_error"] or "",
            "link_status": link_status,
            "recent_solved": recent_list,
            "oj_stats": oj_list,
        }
    finally:
        conn.close()


def vjudge_should_refresh(luogu_uid: str) -> bool:
    """v3.9.74 · 报告生成时判断: 是否需要触发后台刷新(陈旧/失败/限流)。"""
    ctx = get_vjudge_context(luogu_uid)
    return ctx.get("link_status") in ("stale", "failed", "rate_limited")


# ============================================================
# v3.5.2 · 政策匹配学校库（家长版核心模块）
# ============================================================

_POLICY_MATCH_SEED = [
    # ===== 1. 小学 → 当地有科技特长生政策的中学（按城市分组）=====
    # v3.9.8 · 用户要求覆盖全国所有省会 + 直辖市（4 直辖市 + 27 省会/首府）
    # 4 直辖市
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
    # 天津
    ("耀华中学", "tech_talent_junior", "primary", "天津", "天津",
     "信息学特长 +25 分", 40, "CSP-J 二等", "https://www.yaohua.edu.cn/", 10),
    ("南开中学", "tech_talent_junior", "primary", "天津", "天津",
     "科技创新实验班：信息学优先", 50, "GESP 7 级 80+", "https://www.nkzx.cn/", 20),
    # 重庆
    ("重庆巴蜀中学", "tech_talent_junior", "primary", "重庆", "重庆",
     "信息学特长 +30 分", 60, "CSP-J 一等", "https://www.bashu.com.cn/", 10),
    ("重庆一中", "tech_talent_junior", "primary", "重庆", "重庆",
     "创新实验班：信息学 +20 分", 50, "CSP-J 二等", "https://www.cqyz.net/", 20),
    # 河北省会 · 石家庄
    ("石家庄二中", "tech_talent_junior", "primary", "石家庄", "河北",
     "信息学竞赛特长 +20 分", 50, "CSP-J 二等", "http://www.sjzez.com/", 10),
    ("河北师范大学附属中学", "tech_talent_junior", "primary", "石家庄", "河北",
     "科技创新班：信息学优先", 40, "GESP 6 级 80+", "http://www.hbsdfz.com/", 20),
    # 山西省会 · 太原
    ("山西省实验中学", "tech_talent_junior", "primary", "太原", "山西",
     "信息学特长 +25 分", 50, "CSP-J 二等", "http://www.sxssyzx.com/", 10),
    ("太原五中", "tech_talent_junior", "primary", "太原", "山西",
     "创新实验班：信息学 +20 分", 40, "GESP 6 级 80+", "http://www.tywz.cn/", 20),
    # 内蒙古 · 呼和浩特
    ("呼和浩特市第二中学", "tech_talent_junior", "primary", "呼和浩特", "内蒙古",
     "信息学特长 +20 分", 30, "CSP-J 二等", "http://www.hhht2z.com/", 10),
    # 辽宁省会 · 沈阳
    ("东北育才学校", "tech_talent_junior", "primary", "沈阳", "辽宁",
     "信息学特长 +30 分", 60, "CSP-J 一等", "http://www.dbys.com.cn/", 10),
    ("辽宁省实验中学", "tech_talent_junior", "primary", "沈阳", "辽宁",
     "科技特长生 +25 分", 50, "CSP-J 二等", "http://www.lnssyzx.com/", 20),
    # 吉林省会 · 长春
    ("东北师范大学附属中学", "tech_talent_junior", "primary", "长春", "吉林",
     "信息学竞赛特长 +30 分", 60, "CSP-J 一等", "http://www.sdfz.edu.cn/", 10),
    ("长春吉大附中", "tech_talent_junior", "primary", "长春", "吉林",
     "创新实验班：信息学 +20 分", 40, "CSP-J 二等", "http://www.jdfz.cn/", 20),
    # 黑龙江省会 · 哈尔滨
    ("哈尔滨第三中学", "tech_talent_junior", "primary", "哈尔滨", "黑龙江",
     "信息学竞赛特长 +30 分", 60, "CSP-J 一等", "http://www.hrb3z.cn/", 10),
    ("哈尔滨工业大学附属中学", "tech_talent_junior", "primary", "哈尔滨", "黑龙江",
     "科技特长生 +25 分", 50, "CSP-J 二等", "http://www.hitfz.cn/", 20),
    # 江苏省会 · 南京
    ("南京外国语学校", "tech_talent_junior", "primary", "南京", "江苏",
     "科技特长生（信息学）30 分", 40, "CSP-J 一等", "https://www.nfls.com.cn/", 10),
    ("南京树人学校", "tech_talent_junior", "primary", "南京", "江苏",
     "信息学特长 20 分", 30, "CSP-J 二等", "https://www.njshuren.cn/", 20),
    # 浙江省会 · 杭州
    ("杭州外国语学校", "tech_talent_junior", "primary", "杭州", "浙江",
     "科技特长生：信息学省一 50 分", 40, "GESP 8 级 80+", "https://www.hwfls.com/", 10),
    ("建兰中学", "tech_talent_junior", "primary", "杭州", "浙江",
     "科技特长生：信息学省二 20 分", 30, "GESP 7 级 80+", "https://www.jianlanedu.com/", 20),
    # 安徽省会 · 合肥
    ("合肥一中", "tech_talent_junior", "primary", "合肥", "安徽",
     "信息学竞赛特长 +25 分", 50, "CSP-J 二等", "http://www.hfyz.net/", 10),
    ("合肥八中", "tech_talent_junior", "primary", "合肥", "安徽",
     "科技创新班：信息学 +20 分", 40, "GESP 6 级 80+", "http://www.hfbz.cn/", 20),
    # 福建省会 · 福州
    ("福州一中", "tech_talent_junior", "primary", "福州", "福建",
     "信息学特长 +25 分", 50, "CSP-J 二等", "http://www.fzyz.cn/", 10),
    ("福建师范大学附属中学", "tech_talent_junior", "primary", "福州", "福建",
     "创新实验班：信息学 +20 分", 40, "GESP 6 级 80+", "http://www.fjsdfz.cn/", 20),
    # 江西省会 · 南昌
    ("南昌十中", "tech_talent_junior", "primary", "南昌", "江西",
     "信息学竞赛特长 +20 分", 30, "CSP-J 二等", "http://www.jxnc10z.com/", 10),
    ("江西师范大学附属中学", "tech_talent_junior", "primary", "南昌", "江西",
     "科技创新班：信息学优先", 40, "GESP 6 级 80+", "http://www.jxsdfz.com/", 20),
    # 山东省会 · 济南
    ("山东省实验中学", "tech_talent_junior", "primary", "济南", "山东",
     "信息学特长 +30 分", 50, "CSP-J 一等", "http://www.sdssyzx.cn/", 10),
    ("历城二中", "tech_talent_junior", "primary", "济南", "山东",
     "科技特长生 +20 分", 40, "CSP-J 二等", "http://www.lcez.cn/", 20),
    # 河南省会 · 郑州
    ("郑州外国语学校", "tech_talent_junior", "primary", "郑州", "河南",
     "信息学竞赛特长 +30 分", 60, "CSP-J 一等", "http://www.zzfls.cn/", 10),
    ("郑州一中", "tech_talent_junior", "primary", "郑州", "河南",
     "创新实验班：信息学 +25 分", 50, "CSP-J 二等", "http://www.zz1z.cn/", 20),
    # 湖北省会 · 武汉
    ("华中师范大学第一附属中学", "tech_talent_junior", "primary", "武汉", "湖北",
     "信息学特长 +30 分", 60, "CSP-J 一等", "http://www.hzsdfz.com/", 10),
    ("武汉二中", "tech_talent_junior", "primary", "武汉", "湖北",
     "科技特长生 +20 分", 40, "CSP-J 二等", "http://www.whez.cn/", 20),
    # 湖南省会 · 长沙
    ("长沙市长郡中学", "tech_talent_junior", "primary", "长沙", "湖南",
     "信息学竞赛特长 +30 分", 60, "CSP-J 一等", "http://www.cjzxedu.com/", 10),
    ("雅礼中学", "tech_talent_junior", "primary", "长沙", "湖南",
     "科技特长生 +25 分", 50, "CSP-J 二等", "http://www.ylzxedu.com/", 20),
    # 广东省会 · 广州
    ("华南师范大学附属中学", "tech_talent_junior", "primary", "广州", "广东",
     "信息学特长 +30 分", 60, "CSP-J 一等", "http://www.hsfz.net.cn/", 10),
    ("广东实验中学", "tech_talent_junior", "primary", "广州", "广东",
     "创新实验班：信息学 +20 分", 50, "CSP-J 二等", "http://www.gdsyzx.edu.cn/", 20),
    # 广西 · 南宁
    ("南宁二中", "tech_talent_junior", "primary", "南宁", "广西",
     "信息学竞赛特长 +20 分", 30, "CSP-J 二等", "http://www.nnez.com.cn/", 10),
    ("南宁三中", "tech_talent_junior", "primary", "南宁", "广西",
     "科技创新班：信息学优先", 40, "GESP 6 级 80+", "http://www.nnsz.com.cn/", 20),
    # 海南省 · 海口
    ("海南中学", "tech_talent_junior", "primary", "海口", "海南",
     "信息学竞赛特长 +20 分", 30, "CSP-J 二等", "http://www.haizhong.cn/", 10),
    ("海南华侨中学", "tech_talent_junior", "primary", "海口", "海南",
     "科技特长生 +20 分", 40, "GESP 6 级 80+", "http://www.hnqzx.com/", 20),
    # 四川省会 · 成都
    ("成都七中育才学校", "tech_talent_junior", "primary", "成都", "四川",
     "网班：信息学特长优先", 50, "CSP-J 一等", "https://www.cdyucai.com/", 10),
    ("成都石室中学（北湖校区）", "tech_talent_junior", "primary", "成都", "四川",
     "科技特长生 25 分", 30, "CSP-J 二等", "https://www.cd-yucai.cn/", 20),
    # 贵州省 · 贵阳
    ("贵阳一中", "tech_talent_junior", "primary", "贵阳", "贵州",
     "信息学竞赛特长 +20 分", 30, "CSP-J 二等", "http://www.gyyz.cn/", 10),
    # 云南省 · 昆明
    ("云南师范大学附属中学", "tech_talent_junior", "primary", "昆明", "云南",
     "信息学竞赛特长 +25 分", 50, "CSP-J 二等", "http://www.ynsdfz.net/", 10),
    ("昆明一中", "tech_talent_junior", "primary", "昆明", "云南",
     "科技创新班：信息学 +20 分", 40, "GESP 6 级 80+", "http://www.kmyz.cn/", 20),
    # 西藏 · 拉萨
    ("拉萨中学", "tech_talent_junior", "primary", "拉萨", "西藏",
     "科技特长生 +20 分（建议联系当地教育局获取当年简章）", 20, "GESP 6 级 80+", "http://www.lszx.net.cn/", 10),
    # 陕西省 · 西安
    ("西安交通大学附属中学", "tech_talent_junior", "primary", "西安", "陕西",
     "信息学竞赛特长 +30 分", 60, "CSP-J 一等", "http://www.xajdfz.cn/", 10),
    ("西工大附中", "tech_talent_junior", "primary", "西安", "陕西",
     "科技特长生 +25 分", 50, "CSP-J 二等", "http://www.xgdfz.cn/", 20),
    # 甘肃省 · 兰州
    ("西北师范大学附属中学", "tech_talent_junior", "primary", "兰州", "甘肃",
     "信息学竞赛特长 +20 分", 30, "CSP-J 二等", "http://www.nwnufz.cn/", 10),
    # 青海省 · 西宁
    ("青海湟川中学", "tech_talent_junior", "primary", "西宁", "青海",
     "科技特长生 +20 分（建议联系当地教育局获取当年简章）", 20, "GESP 6 级 80+", "http://www.qhhczx.com/", 10),
    # 宁夏 · 银川
    ("银川一中", "tech_talent_junior", "primary", "银川", "宁夏",
     "信息学竞赛特长 +20 分", 30, "CSP-J 二等", "http://www.ycyz.cn/", 10),
    # 新疆 · 乌鲁木齐
    ("乌鲁木齐市第一中学", "tech_talent_junior", "primary", "乌鲁木齐", "新疆",
     "信息学竞赛特长 +20 分", 30, "CSP-J 二等", "http://www.wlmqyz.cn/", 10),
    # 特别行政区（港澳 - 走 DSE 体系，附简单参考）
    ("香港拔萃男书院", "tech_talent_junior", "primary", "香港", "香港",
     "DSE 体系 · 信息学奥赛成绩可申请 STEM 奖学金", 30, "DSE 5*+", "https://www.dbs.edu.hk/", 10),
    ("澳门濠江中学", "tech_talent_junior", "primary", "澳门", "澳门",
     "DSE / 内地高考双轨 · 信息学奖项 +10 分", 30, "CSP-J 二等+", "https://www.houkong.edu.mo/", 10),

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
    # 天津
    ("耀华中学", "self_enroll_senior", "junior", "天津", "天津",
     "自招：信息学省二 25 分", 50, "CSP-S 二等", "https://www.yaohua.edu.cn/", 10),
    ("南开中学", "self_enroll_senior", "junior", "天津", "天津",
     "自招：信息学省一 30 分", 60, "CSP-S 一等", "https://www.nkzx.cn/", 20),
    # 重庆
    ("重庆巴蜀中学", "self_enroll_senior", "junior", "重庆", "重庆",
     "自招：信息学省一 30 分", 60, "CSP-S 一等", "https://www.bashu.com.cn/", 10),
    ("重庆一中", "self_enroll_senior", "junior", "重庆", "重庆",
     "自招：信息学省二 20 分", 50, "CSP-S 二等", "https://www.cqyz.net/", 20),
    # 石家庄
    ("石家庄二中", "self_enroll_senior", "junior", "石家庄", "河北",
     "自招：信息学省二 20 分", 50, "CSP-S 二等", "http://www.sjzez.com/", 10),
    ("衡水中学", "self_enroll_senior", "junior", "石家庄", "河北",
     "自招：信息学省一 30 分（衡水体系）", 60, "CSP-S 一等", "https://www.hshs.cn/", 20),
    # 太原
    ("山西省实验中学", "self_enroll_senior", "junior", "太原", "山西",
     "自招：信息学省二 25 分", 50, "CSP-S 二等", "http://www.sxssyzx.com/", 10),
    # 呼和浩特
    ("呼和浩特市第二中学", "self_enroll_senior", "junior", "呼和浩特", "内蒙古",
     "自招：信息学省二 20 分", 40, "CSP-S 二等", "http://www.hhht2z.com/", 10),
    # 沈阳
    ("东北育才学校", "self_enroll_senior", "junior", "沈阳", "辽宁",
     "自招：信息学省一 30 分", 60, "CSP-S 一等", "http://www.dbys.com.cn/", 10),
    ("辽宁省实验中学", "self_enroll_senior", "junior", "沈阳", "辽宁",
     "自招：信息学省二 25 分", 50, "CSP-S 二等", "http://www.lnssyzx.com/", 20),
    # 长春
    ("东北师范大学附属中学", "self_enroll_senior", "junior", "长春", "吉林",
     "自招：信息学省一 30 分", 60, "CSP-S 一等", "http://www.sdfz.edu.cn/", 10),
    ("长春吉大附中", "self_enroll_senior", "junior", "长春", "吉林",
     "自招：信息学省二 25 分", 50, "CSP-S 二等", "http://www.jdfz.cn/", 20),
    # 哈尔滨
    ("哈尔滨第三中学", "self_enroll_senior", "junior", "哈尔滨", "黑龙江",
     "自招：信息学省一 30 分", 60, "CSP-S 一等", "http://www.hrb3z.cn/", 10),
    # 南京
    ("南京外国语学校", "self_enroll_senior", "junior", "南京", "江苏",
     "自招：信息学省一 30 分", 60, "CSP-S 一等", "https://www.nfls.com.cn/", 10),
    ("南京师范大学附属中学", "self_enroll_senior", "junior", "南京", "江苏",
     "自招：信息学省一 30 分", 50, "CSP-S 一等", "https://www.nsfz.net/", 20),
    # 杭州
    ("杭州第二中学（滨江校区）", "self_enroll_senior", "junior", "杭州", "浙江",
     "自招：信息学省一 30 分", 80, "CSP-S 一等", "https://www.hz2hs.net.cn/", 10),
    ("学军中学（紫金港校区）", "self_enroll_senior", "junior", "杭州", "浙江",
     "自招：信息学省一 30 分", 60, "CSP-S 一等", "https://www.xjhs.cn/", 20),
    ("杭州外国语学校", "self_enroll_senior", "junior", "杭州", "浙江",
     "自招：信息学省二 20 分", 50, "CSP-S 二等", "https://www.hwfls.com/", 30),
    # 合肥
    ("合肥一中", "self_enroll_senior", "junior", "合肥", "安徽",
     "自招：信息学省二 25 分", 50, "CSP-S 二等", "http://www.hfyz.net/", 10),
    # 福州
    ("福州一中", "self_enroll_senior", "junior", "福州", "福建",
     "自招：信息学省二 25 分", 50, "CSP-S 二等", "http://www.fzyz.cn/", 10),
    # 南昌
    ("江西师范大学附属中学", "self_enroll_senior", "junior", "南昌", "江西",
     "自招：信息学省二 20 分", 40, "CSP-S 二等", "http://www.jxsdfz.com/", 10),
    # 济南
    ("山东省实验中学", "self_enroll_senior", "junior", "济南", "山东",
     "自招：信息学省一 30 分", 60, "CSP-S 一等", "http://www.sdssyzx.cn/", 10),
    # 郑州
    ("郑州外国语学校", "self_enroll_senior", "junior", "郑州", "河南",
     "自招：信息学省一 30 分", 60, "CSP-S 一等", "http://www.zzfls.cn/", 10),
    ("郑州一中", "self_enroll_senior", "junior", "郑州", "河南",
     "自招：信息学省二 25 分", 50, "CSP-S 二等", "http://www.zz1z.cn/", 20),
    # 武汉
    ("华中师范大学第一附属中学", "self_enroll_senior", "junior", "武汉", "湖北",
     "自招：信息学省一 30 分", 60, "CSP-S 一等", "http://www.hzsdfz.com/", 10),
    # 长沙
    ("长沙市长郡中学", "self_enroll_senior", "junior", "长沙", "湖南",
     "自招：信息学省一 30 分", 60, "CSP-S 一等", "http://www.cjzxedu.com/", 10),
    ("雅礼中学", "self_enroll_senior", "junior", "长沙", "湖南",
     "自招：信息学省一 30 分", 50, "CSP-S 一等", "http://www.ylzxedu.com/", 20),
    # 广州
    ("华南师范大学附属中学", "self_enroll_senior", "junior", "广州", "广东",
     "自招：信息学省一 30 分", 60, "CSP-S 一等", "http://www.hsfz.net.cn/", 10),
    ("广东实验中学", "self_enroll_senior", "junior", "广州", "广东",
     "自招：信息学省一 30 分", 50, "CSP-S 一等", "http://www.gdsyzx.edu.cn/", 20),
    # 深圳
    ("深圳中学", "self_enroll_senior", "junior", "深圳", "广东",
     "自招：信息学省一 40 分", 60, "CSP-S 一等", "https://www.shenzhong.net/", 10),
    ("深圳实验学校", "self_enroll_senior", "junior", "深圳", "广东",
     "自招：信息学省一 30 分", 50, "CSP-S 一等", "https://www.szsy.cn/", 20),
    # 南宁
    ("南宁二中", "self_enroll_senior", "junior", "南宁", "广西",
     "自招：信息学省二 20 分", 40, "CSP-S 二等", "http://www.nnez.com.cn/", 10),
    # 海口
    ("海南中学", "self_enroll_senior", "junior", "海口", "海南",
     "自招：信息学省二 20 分", 40, "CSP-S 二等", "http://www.haizhong.cn/", 10),
    # 成都
    ("成都七中（林荫校区）", "self_enroll_senior", "junior", "成都", "四川",
     "自招：信息学省一 30 分", 80, "CSP-S 一等", "https://www.cdqz.net/", 10),
    ("成都石室中学（文庙校区）", "self_enroll_senior", "junior", "成都", "四川",
     "自招：信息学省二 20 分", 50, "CSP-S 二等", "https://www.cd-yucai.cn/", 20),
    # 贵阳
    ("贵阳一中", "self_enroll_senior", "junior", "贵阳", "贵州",
     "自招：信息学省二 20 分", 40, "CSP-S 二等", "http://www.gyyz.cn/", 10),
    # 昆明
    ("云南师范大学附属中学", "self_enroll_senior", "junior", "昆明", "云南",
     "自招：信息学省二 25 分", 50, "CSP-S 二等", "http://www.ynsdfz.net/", 10),
    # 拉萨
    ("拉萨中学", "self_enroll_senior", "junior", "拉萨", "西藏",
     "自招：信息学省二 20 分（建议联系当地教育局获取当年简章）", 20, "CSP-S 二等", "http://www.lszx.net.cn/", 10),
    # 西安
    ("西安交通大学附属中学", "self_enroll_senior", "junior", "西安", "陕西",
     "自招：信息学省一 30 分", 60, "CSP-S 一等", "http://www.xajdfz.cn/", 10),
    ("西工大附中", "self_enroll_senior", "junior", "西安", "陕西",
     "自招：信息学省一 30 分", 50, "CSP-S 一等", "http://www.xgdfz.cn/", 20),
    # 兰州
    ("西北师范大学附属中学", "self_enroll_senior", "junior", "兰州", "甘肃",
     "自招：信息学省二 20 分", 40, "CSP-S 二等", "http://www.nwnufz.cn/", 10),
    # 西宁
    ("青海湟川中学", "self_enroll_senior", "junior", "西宁", "青海",
     "自招：信息学省二 20 分（建议联系当地教育局获取当年简章）", 20, "CSP-S 二等", "http://www.qhhczx.com/", 10),
    # 银川
    ("银川一中", "self_enroll_senior", "junior", "银川", "宁夏",
     "自招：信息学省二 20 分", 40, "CSP-S 二等", "http://www.ycyz.cn/", 10),
    # 乌鲁木齐
    ("乌鲁木齐市第一中学", "self_enroll_senior", "junior", "乌鲁木齐", "新疆",
     "自招：信息学省二 20 分", 40, "CSP-S 二等", "http://www.wlmqyz.cn/", 10),
    # 港澳
    ("香港圣保罗男女中学", "self_enroll_senior", "junior", "香港", "香港",
     "DSE 体系 · STEM 资优奖学金", 30, "DSE 5**", "https://www.stpaulsec.edu.hk/", 10),
    ("澳门培正中学", "self_enroll_senior", "junior", "澳门", "澳门",
     "DSE / 内地高考双轨 · 信息学 +10 分", 30, "CSP-S 二等+", "https://www.puiching.edu.mo/", 10),

    # ===== 3. 高中 → 强基 39 校（全国统一）=====
    ("清华大学", "qiangji_university", "senior", "全国", "全国",
     "强基计划：信息学金牌破格入围", 30, "NOI 金牌 / NOIP 省一", "https://www.tsinghua.edu.cn/", 10),
    ("北京大学", "qiangji_university", "senior", "全国", "全国",
     "强基计划：信息学金牌破格入围", 30, "NOI 金牌 / NOIP 省一", "https://www.pku.edu.cn/", 20),
    ("中国人民大学", "qiangji_university", "senior", "全国", "北京",
     "强基计划：信息学省一 + 高考一本线", 20, "NOIP 省一 + 高考一本线", "https://www.ruc.edu.cn/", 30),
    ("北京航空航天大学", "qiangji_university", "senior", "全国", "北京",
     "强基计划：信息学省一 + 高考一本线", 20, "NOIP 省一 + 高考一本线", "https://www.buaa.edu.cn/", 40),
    ("北京理工大学", "qiangji_university", "senior", "全国", "北京",
     "强基计划：信息学省一 + 高考一本线", 20, "NOIP 省一 + 高考一本线", "https://www.bit.edu.cn/", 50),
    ("中国农业大学", "qiangji_university", "senior", "全国", "北京",
     "强基计划：信息学省二 + 高考一本线", 15, "NOIP 省二 + 高考一本线", "https://www.cau.edu.cn/", 60),
    ("北京师范大学", "qiangji_university", "senior", "全国", "北京",
     "强基计划：信息学省二 + 高考一本线", 15, "NOIP 省二 + 高考一本线", "https://www.bnu.edu.cn/", 70),
    ("中央民族大学", "qiangji_university", "senior", "全国", "北京",
     "强基计划：信息学省二 + 高考一本线", 15, "NOIP 省二 + 高考一本线", "https://www.muc.edu.cn/", 80),
    ("南开大学", "qiangji_university", "senior", "全国", "天津",
     "强基计划：信息学省一 + 高考一本线", 20, "NOIP 省一 + 高考一本线", "https://www.nankai.edu.cn/", 90),
    ("天津大学", "qiangji_university", "senior", "全国", "天津",
     "强基计划：信息学省一 + 高考一本线", 20, "NOIP 省一 + 高考一本线", "https://www.tju.edu.cn/", 100),
    ("大连理工大学", "qiangji_university", "senior", "全国", "辽宁",
     "强基计划：信息学省二 + 高考一本线", 15, "NOIP 省二 + 高考一本线", "https://www.dlut.edu.cn/", 110),
    ("东北大学", "qiangji_university", "senior", "全国", "辽宁",
     "强基计划：信息学省二 + 高考一本线", 15, "NOIP 省二 + 高考一本线", "https://www.neu.edu.cn/", 120),
    ("吉林大学", "qiangji_university", "senior", "全国", "吉林",
     "强基计划：信息学省二 + 高考一本线", 15, "NOIP 省二 + 高考一本线", "https://www.jlu.edu.cn/", 130),
    ("哈尔滨工业大学", "qiangji_university", "senior", "全国", "黑龙江",
     "强基计划：信息学省一 + 高考一本线", 20, "NOIP 省一 + 高考一本线", "https://www.hit.edu.cn/", 140),
    ("复旦大学", "qiangji_university", "senior", "全国", "上海",
     "强基计划：信息学省一 + 高考一本线", 20, "NOIP 省一 + 高考一本线", "https://www.fudan.edu.cn/", 150),
    ("同济大学", "qiangji_university", "senior", "全国", "上海",
     "强基计划：信息学省一 + 高考一本线", 20, "NOIP 省一 + 高考一本线", "https://www.tongji.edu.cn/", 160),
    ("上海交通大学", "qiangji_university", "senior", "全国", "上海",
     "强基计划：信息学省一 + 高考一本线", 20, "NOIP 省一 + 高考一本线", "https://www.sjtu.edu.cn/", 170),
    ("华东师范大学", "qiangji_university", "senior", "全国", "上海",
     "强基计划：信息学省二 + 高考一本线", 15, "NOIP 省二 + 高考一本线", "https://www.ecnu.edu.cn/", 180),
    ("南京大学", "qiangji_university", "senior", "全国", "江苏",
     "强基计划：信息学省一 + 高考一本线", 20, "NOIP 省一 + 高考一本线", "https://www.nju.edu.cn/", 190),
    ("东南大学", "qiangji_university", "senior", "全国", "江苏",
     "强基计划：信息学省一 + 高考一本线", 20, "NOIP 省一 + 高考一本线", "https://www.seu.edu.cn/", 200),
    ("浙江大学", "qiangji_university", "senior", "全国", "浙江",
     "强基计划：信息学省一 + 高考一本线", 20, "NOIP 省一 + 高考一本线", "https://www.zju.edu.cn/", 210),
    ("中国科学技术大学", "qiangji_university", "senior", "全国", "安徽",
     "强基计划：信息学金牌破格入围", 25, "NOI 金牌 / NOIP 省一", "https://www.ustc.edu.cn/", 220),
    ("厦门大学", "qiangji_university", "senior", "全国", "福建",
     "强基计划：信息学省二 + 高考一本线", 15, "NOIP 省二 + 高考一本线", "https://www.xmu.edu.cn/", 230),
    ("山东大学", "qiangji_university", "senior", "全国", "山东",
     "强基计划：信息学省二 + 高考一本线", 15, "NOIP 省二 + 高考一本线", "https://www.sdu.edu.cn/", 240),
    ("中国海洋大学", "qiangji_university", "senior", "全国", "山东",
     "强基计划：信息学省二 + 高考一本线", 15, "NOIP 省二 + 高考一本线", "https://www.ouc.edu.cn/", 250),
    ("武汉大学", "qiangji_university", "senior", "全国", "湖北",
     "强基计划：信息学省一 + 高考一本线", 20, "NOIP 省一 + 高考一本线", "https://www.whu.edu.cn/", 260),
    ("华中科技大学", "qiangji_university", "senior", "全国", "湖北",
     "强基计划：信息学省一 + 高考一本线", 20, "NOIP 省一 + 高考一本线", "https://www.hust.edu.cn/", 270),
    ("中南大学", "qiangji_university", "senior", "全国", "湖南",
     "强基计划：信息学省二 + 高考一本线", 15, "NOIP 省二 + 高考一本线", "https://www.csu.edu.cn/", 280),
    ("湖南大学", "qiangji_university", "senior", "全国", "湖南",
     "强基计划：信息学省二 + 高考一本线", 15, "NOIP 省二 + 高考一本线", "https://www.hnu.edu.cn/", 290),
    ("国防科技大学", "qiangji_university", "senior", "全国", "湖南",
     "强基计划：信息学金牌破格入围", 20, "NOI 金牌 / NOIP 省一", "https://www.nudt.edu.cn/", 300),
    ("中山大学", "qiangji_university", "senior", "全国", "广东",
     "强基计划：信息学省一 + 高考一本线", 20, "NOIP 省一 + 高考一本线", "https://www.sysu.edu.cn/", 310),
    ("华南理工大学", "qiangji_university", "senior", "全国", "广东",
     "强基计划：信息学省一 + 高考一本线", 20, "NOIP 省一 + 高考一本线", "https://www.scut.edu.cn/", 320),
    ("四川大学", "qiangji_university", "senior", "全国", "四川",
     "强基计划：信息学省二 + 高考一本线", 15, "NOIP 省二 + 高考一本线", "https://www.scu.edu.cn/", 330),
    ("重庆大学", "qiangji_university", "senior", "全国", "重庆",
     "强基计划：信息学省二 + 高考一本线", 15, "NOIP 省二 + 高考一本线", "https://www.cqu.edu.cn/", 340),
    ("电子科技大学", "qiangji_university", "senior", "全国", "四川",
     "强基计划：信息学省一 + 高考一本线", 20, "NOIP 省一 + 高考一本线", "https://www.uestc.edu.cn/", 350),
    ("西安交通大学", "qiangji_university", "senior", "全国", "陕西",
     "强基计划：信息学省一 + 高考一本线", 20, "NOIP 省一 + 高考一本线", "https://www.xjtu.edu.cn/", 360),
    ("西北工业大学", "qiangji_university", "senior", "全国", "陕西",
     "强基计划：信息学省一 + 高考一本线", 20, "NOIP 省一 + 高考一本线", "https://www.nwpu.edu.cn/", 370),
    ("西北农林科技大学", "qiangji_university", "senior", "全国", "陕西",
     "强基计划：信息学省二 + 高考一本线", 10, "NOIP 省二 + 高考一本线", "https://www.nwafu.edu.cn/", 380),
    ("兰州大学", "qiangji_university", "senior", "全国", "甘肃",
     "强基计划：信息学省二 + 高考一本线", 10, "NOIP 省二 + 高考一本线", "https://www.lzu.edu.cn/", 390),
]


def _seed_policy_match_schools(conn):
    """种子数据：政策匹配学校库

    v3.9.8 · 用户要求覆盖全国所有省会 + 直辖市
      - 4 直辖市（北京/上海/天津/重庆）
      - 27 省会/自治区首府（石家庄/太原/呼和浩特/沈阳/长春/哈尔滨/南京/杭州/合肥/福州/南昌/济南/郑州/武汉/长沙/广州/南宁/海口/成都/贵阳/昆明/拉萨/西安/兰州/西宁/银川/乌鲁木齐）
      - 2 特别行政区（香港/澳门，DSE 体系参考）
      - 39 所强基大学（全国统一）
    v3.9.8 · 修复：当条目数变化时，仅新增缺失的条目（不去重已存在的），避免升级时把已经手动维护的删掉
    """
    cur = conn.execute("SELECT COUNT(*) FROM policy_match_schools")
    if cur.fetchone()[0] >= len(_POLICY_MATCH_SEED):
        # 已存在的条目数 >= 新 seed 列表长度 → 完整重种子会破坏已有数据
        # 仅补充缺失的（按 school_name 唯一）
        existing_names = {
            (r[0] if r else "")
            for r in conn.execute("SELECT school_name FROM policy_match_schools").fetchall()
        }
        added = 0
        for row in _POLICY_MATCH_SEED:
            if row[0] not in existing_names:
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
                    added += 1
                except sqlite3.IntegrityError:
                    pass
        if added:
            conn.commit()
        return
    # 首次 seed
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


def get_latest_done_task_for_uid(luogu_uid: str, since_hours: int = 24, task_type: str = "") -> dict | None:
    """v3.8 · 查最近 N 小时内该 UID 是否已生成过报告（用于每日 1 次限流）

    Args:
        luogu_uid: 洛谷 UID（字符串）
        since_hours: 限定 N 小时内（默认 24）
        task_type: 报告类型过滤（v3.9.64 · report_noi_csp / report_gesp）。
                   传空字符串表示不限类型（任意报告都算）。

    Returns:
        若存在已完成的报告任务，返回该任务字典；
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
        # v3.9.64 · 按 task_type 过滤（不同报告类型互不限制）
        _type_filter = ""
        _params: list = [uid, threshold]
        if task_type and "task_type" in cols:
            _type_filter = " AND t.task_type = ?"
            _params.append(str(task_type))
        row = conn.execute(
            f"""
            SELECT t.task_id, t.status, t.created_at, t.html, t.student_name
            FROM tasks t
            WHERE t.luogu_uid = ?
              AND t.status IN ('done', 'partial')
              AND (t.created_at IS NULL OR t.created_at >= ?)
              {_type_filter}
            ORDER BY t.created_at DESC
            LIMIT 1
            """,
            tuple(_params),
        ).fetchone()
        if not row:
            return None
        return dict(row)
    finally:
        conn.close()


def insert_task(task_id: str, status: str = "queued", message: str = "排队中...", luogu_uid: str = "", task_type: str = ""):
    """v3.9.64 · 新增 task_type 参数（report_noi_csp / report_gesp），用于按报告类型限流。"""
    conn = _get_conn()
    try:
        # v3.8 · 幂等添加 luogu_uid 列（用于每日 1 次生成限制）
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN luogu_uid TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # 列已存在
        # v3.9.64 · 幂等添加 task_type 列
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN task_type TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_luogu_uid ON tasks(luogu_uid)")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_task_type ON tasks(task_type)")
        except sqlite3.OperationalError:
            pass
        # 兜底：检查 luogu_uid / task_type 列存在性，老库可能完全没这些列
        _cols = [r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
        _has_luogu_uid = "luogu_uid" in _cols
        _has_task_type = "task_type" in _cols
        _col_names = ["task_id", "status", "message", "created_at"]
        _col_vals: list = [task_id, status, message, datetime.now().strftime("%Y-%m-%d %H:%M:%S")]
        if _has_luogu_uid:
            _col_names.append("luogu_uid")
            _col_vals.append(str(luogu_uid or "").strip())
        if _has_task_type:
            _col_names.append("task_type")
            _col_vals.append(str(task_type or "").strip())
        _placeholders = ",".join(["?"] * len(_col_names))
        conn.execute(
            f"INSERT OR IGNORE INTO tasks ({','.join(_col_names)}) VALUES ({_placeholders})",
            _col_vals,
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


# =============================================================================
# v3.11 · 学员主动上传的代码(书签导出 / zip / JSON / 粘贴)
# 数据是学员本机抓取自己提交后主动 POST 到服务端,合规
# =============================================================================

# 单次导入上限:防止学员不小心塞了整个桌面
_IMPORT_MAX_PER_REQUEST = 2000
# 单条代码大小上限:50KB
_IMPORT_MAX_CODE_BYTES = 50 * 1024


def import_student_codes(student_id: int, records: list[dict], source: str = "upload") -> dict:
    """v3.11 · 批量导入学员代码(去重 upsert)。
    records: [{pid/problem_id, title, lang, verdict, time, memory, code, ac_time?}, ...]
    source: 'bookmarklet' / 'json_upload' / 'zip_upload' / 'paste'
    返回: {imported, skipped, total}
    """
    if not records:
        return {"imported": 0, "skipped": 0, "total": 0, "error": "空列表"}

    if len(records) > _IMPORT_MAX_PER_REQUEST:
        return {
            "imported": 0, "skipped": 0, "total": len(records),
            "error": f"单次最多 {_IMPORT_MAX_PER_REQUEST} 条,当前 {len(records)} 条",
        }

    conn = _get_conn()
    imported = 0
    skipped = 0
    try:
        for r in records:
            code = (r.get("code") or r.get("sourceCode") or "").strip()
            if not code:
                skipped += 1
                continue
            if len(code.encode("utf-8", errors="ignore")) > _IMPORT_MAX_CODE_BYTES:
                skipped += 1
                continue
            try:
                cur = conn.execute("""
                    INSERT OR IGNORE INTO student_imported_codes
                    (student_id, problem_id, title, lang, verdict, time_ms, memory_kb,
                     source, source_code, ac_time)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    student_id,
                    (r.get("pid") or r.get("problem_id") or "").strip()[:50],
                    (r.get("title") or "").strip()[:200],
                    (r.get("lang") or "").strip()[:20],
                    (r.get("verdict") or "").strip()[:20],
                    int(r.get("time") or r.get("time_ms") or 0),
                    int(r.get("memory") or r.get("memory_kb") or 0),
                    source[:20],
                    code,
                    r.get("ac_time") or r.get("submitTime") or None,
                ))
                # cur.rowcount: 1 = inserted, 0 = ignored (UNIQUE conflict)
                if cur.rowcount > 0:
                    imported += 1
                else:
                    skipped += 1
            except Exception:
                skipped += 1
        conn.commit()
    finally:
        conn.close()
    return {"imported": imported, "skipped": skipped, "total": len(records)}


def get_imported_codes_summary(student_id: int) -> dict:
    """v3.11 · 取学员已上传代码的统计摘要(给 UI 卡片用)。"""
    conn = _get_conn()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM student_imported_codes WHERE student_id=?",
            (student_id,),
        ).fetchone()[0]
        by_lang = dict(conn.execute(
            """SELECT lang, COUNT(*) FROM student_imported_codes
               WHERE student_id=? AND lang!='' GROUP BY lang ORDER BY 2 DESC LIMIT 10""",
            (student_id,),
        ).fetchall())
        verdict = dict(conn.execute(
            """SELECT verdict, COUNT(*) FROM student_imported_codes
               WHERE student_id=? AND verdict!='' GROUP BY verdict""",
            (student_id,),
        ).fetchall())
        last_import = conn.execute(
            """SELECT MAX(imported_at) FROM student_imported_codes WHERE student_id=?""",
            (student_id,),
        ).fetchone()[0]
        last_source = conn.execute(
            """SELECT source, MAX(imported_at) FROM student_imported_codes
               WHERE student_id=? GROUP BY source ORDER BY 2 DESC LIMIT 1""",
            (student_id,),
        ).fetchone()
        return {
            "total": total or 0,
            "by_lang": by_lang,
            "verdict": verdict,
            "last_import": last_import or "",
            "last_source": (last_source[0] if last_source else "") or "",
        }
    finally:
        conn.close()


def get_imported_codes_for_report(student_id: int, limit: int = 300) -> list[dict]:
    """v3.11 · 给 AI 报告喂数据:取最近 N 条上传的代码(精简字段,避免 prompt 爆炸)。"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT problem_id, title, lang, verdict, time_ms, memory_kb, source_code
               FROM student_imported_codes WHERE student_id=?
               ORDER BY imported_at DESC LIMIT ?""",
            (student_id, int(limit)),
        ).fetchall()
        out = []
        for r in rows:
            code = (r["source_code"] or "")[:3000]  # 单条截 3KB
            out.append({
                "pid": r["problem_id"],
                "title": r["title"],
                "lang": r["lang"],
                "verdict": r["verdict"],
                "code": code,
            })
        return out
    finally:
        conn.close()


# 初始化
init_db()


# v3.9.73 · AtCoder 5 张表(幂等,可重跑)+ 容器启动时清理孤儿任务
init_atcoder_tables()
try:
    _recovered = recover_orphan_atcoder_tasks()
    if _recovered:
        print(f"[v3.9.73] atcoder orphan tasks recovered: {_recovered}")
except Exception as _e:
    print(f"[v3.9.73] orphan recovery warning: {_e}")


# v3.9.74 · VJudge 4 张表(取代 AtCoder,只抓公开数据)+ 清理孤儿任务
init_vjudge_tables()
try:
    _recovered_vj = recover_orphan_vjudge_tasks()
    if _recovered_vj:
        print(f"[v3.9.74] vjudge orphan tasks recovered: {_recovered_vj}")
except Exception as _e:
    print(f"[v3.9.74] vjudge orphan recovery warning: {_e}")


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
        # v3.9.74 VJudge 4 表
        "student_vjudge_data",
        "student_vjudge_solved",
        "student_vjudge_fetch_tasks",
        "student_vjudge_oj_stats",
        # v3.11 学员上传代码
        "student_imported_codes",
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

