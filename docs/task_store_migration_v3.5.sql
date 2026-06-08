-- task_store_migration_v3.5.sql
-- v3.5 新增 4 张表 + students 扩展字段 + 2 张新业务表
-- 替代 v2 的 task_store.py schema

-- ===================================================================
-- 1. 赛事主表（年度数据，CCF 12 月公布下一年）
-- ===================================================================
CREATE TABLE IF NOT EXISTS competitions (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  code                  TEXT UNIQUE NOT NULL,        -- 'GESP-2026-03-L5'
  name                  TEXT NOT NULL,
  type                  TEXT NOT NULL,               -- 'gesp' / 'csp_j1' / 'csp_s1' / 'csp_j2' / 'csp_s2' / 'noip' / 'noi' / 'wc' / 'apio' / 'ctsc' / 'provincial'
  level                 INTEGER,                     -- GESP 1-8 / 0=无级别
  exam_date             DATE NOT NULL,
  registration_deadline DATE,
  location              TEXT,
  target_audience       TEXT,                        -- '小学 5-6 年级' / '初中' / '高中' / '强基' / '中考'
  fee_cny               INTEGER DEFAULT 0,
  source_url            TEXT,
  data_year             INTEGER NOT NULL,
  notes                 TEXT,
  updated_at            DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_comp_date ON competitions(exam_date);
CREATE INDEX IF NOT EXISTS idx_comp_type_year ON competitions(type, data_year);

-- ===================================================================
-- 2. 学员 vs 赛事 关联（家长订阅核心）
-- ===================================================================
CREATE TABLE IF NOT EXISTS student_competitions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id      INTEGER NOT NULL,
  competition_id  INTEGER NOT NULL,
  registered      BOOLEAN DEFAULT 0,
  target_score    INTEGER,
  target_rank     TEXT,
  actual_score    INTEGER,
  actual_rank     TEXT,
  result_level    TEXT,                             -- 'passed' / 'failed' / 'absent' / 'unknown'
  notes           TEXT,
  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(student_id, competition_id)
);
CREATE INDEX IF NOT EXISTS idx_sc_student ON student_competitions(student_id);

-- ===================================================================
-- 3. 学员 GESP 考试记录（hard fact，区别于 AI 估算）
-- ===================================================================
CREATE TABLE IF NOT EXISTS gesp_exams (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id        INTEGER NOT NULL,
  exam_id           INTEGER NOT NULL,               -- → competitions.id
  registered_level  INTEGER NOT NULL,               -- 报的几级（1-8）
  actual_score      INTEGER,                         -- 0-100
  passed            BOOLEAN,                         -- >=60
  can_skip_next     BOOLEAN DEFAULT 0,               -- >=90 触发跳级
  exempts_csp_j     BOOLEAN DEFAULT 0,               -- 7级80+ / 8级60+
  exempts_csp_s     BOOLEAN DEFAULT 0,               -- 8级80+
  certificate_no    TEXT,
  notes             TEXT,
  recorded_by       TEXT,                            -- 'admin' / 'self' / 'parent'
  created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(student_id, exam_id)
);
CREATE INDEX IF NOT EXISTS idx_gesp_student_level ON gesp_exams(student_id, registered_level);

-- ===================================================================
-- 4. 政策日历（强基 / 中考 / 高考 / 强基校测 / 综评）
-- ===================================================================
CREATE TABLE IF NOT EXISTS policy_events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  event_code      TEXT UNIQUE,
  name            TEXT NOT NULL,
  category        TEXT,                             -- 'qiangji' / 'zizhao' / 'zongping' / 'baosong' / 'gaokao' / 'zk_zhongkao'
  event_date      DATE,
  target_audience TEXT,
  source_url      TEXT,
  description     TEXT,
  data_year       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_policy_date ON policy_events(event_date);
CREATE INDEX IF NOT EXISTS idx_policy_category_year ON policy_events(category, data_year);

-- ===================================================================
-- 5. 学员档案扩展字段（增量 ALTER，保留 v2 风格的容错）
-- ===================================================================
-- 注：SQLite 不支持 IF NOT EXISTS on ADD COLUMN，需要 try/except
-- task_store.py 中通过 _safe_alter() 包装

ALTER TABLE students ADD COLUMN gesp_highest_passed INTEGER DEFAULT 0;
ALTER TABLE students ADD COLUMN gesp_latest_score INTEGER;
ALTER TABLE students ADD COLUMN gesp_can_exempt_csp_j BOOLEAN DEFAULT 0;
ALTER TABLE students ADD COLUMN gesp_can_exempt_csp_s BOOLEAN DEFAULT 0;
ALTER TABLE students ADD COLUMN gesp_exemption_expiry DATE;
ALTER TABLE students ADD COLUMN gesp_next_eligible_level INTEGER;

-- ===================================================================
-- 6. 家长表（家长订阅核心）
-- ===================================================================
CREATE TABLE IF NOT EXISTS guardians (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id            INTEGER NOT NULL,
  phone                 TEXT,
  email                 TEXT,
  display_name          TEXT,                        -- "张同学家长"
  notify_channel        TEXT,                        -- 'email' / 'sms' / 'wechat' / 'none'
  notify_token          TEXT UNIQUE,
  notify_token_expires_at DATETIME,
  consent_ip            TEXT,                        -- PIPL 授权 IP（v3.5 P1 必填）
  created_at            DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_guardian_student ON guardians(student_id);

-- ===================================================================
-- 7. 学员周报
-- ===================================================================
CREATE TABLE IF NOT EXISTS weekly_reports (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id      INTEGER NOT NULL,
  week_start      DATE,
  html_path       TEXT,
  pdf_path        TEXT,
  delivered_at    DATETIME,
  open_count      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_weekly_student ON weekly_reports(student_id, week_start);

-- ===================================================================
-- 8. 学员目标路径
-- ===================================================================
CREATE TABLE IF NOT EXISTS student_goals (
  student_id          INTEGER PRIMARY KEY,
  primary_path        TEXT,                         -- '保送' / '强基' / '综评' / '文化课保底' / '兴趣探索' / '未决定'
  target_university   TEXT,
  target_province     TEXT,
  notes               TEXT,
  updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ===================================================================
-- 9. 付费激活码（v3.5 Month 3）
-- ===================================================================
CREATE TABLE IF NOT EXISTS activation_codes (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  code            TEXT UNIQUE NOT NULL,
  sku             TEXT NOT NULL,                    -- 'student_pro' / 'parent' / 'camp_j' / 'camp_s'
  duration_days   INTEGER NOT NULL,
  student_id      INTEGER,
  redeemed_at     DATETIME,
  expires_at      DATETIME,
  created_by      TEXT,
  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_act_code ON activation_codes(code);
