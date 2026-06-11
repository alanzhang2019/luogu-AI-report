# 报告预览中转页 + 隐藏 PDF 分享渠道重构 · v3.7 设计

> **状态**：已批准，待实施
> **日期**：2026-06-11
> **作者**：GStack 协作（office-hours → brainstorming）
> **范围**：v3.7 单次交付，不含奖励机制（v3.7.1）

---

## 1. 背景与目标

### 1.1 现状
- 报告生成后，**自动产出 4 份 PDF + 4 份 HTML + 1 份 MD**，可任意下载。
- 海报 PNG 底部二维码指向 `/me/<luogu_uid>`（个人中心），陌生人扫码进入"轻量版个人中心"。
- 用户分享报告的方式是 **下载 PDF 发文件**（无追踪、无门槛、无引导新用户）。
- 海报分享流与个人中心流**没有分离**，新用户体验"半成品"。

### 1.2 目标（v3.7）
1. **隐藏 PDF 版本**：报告 PDF 对外不开放（仅生成、不下载、不发链接）。
2. **统一分享渠道**：用户只能通过**海报 PNG（二维码）**分享报告。
3. **新用户体验报告**：扫码进入**专门的中转页**，看到"摘要 + 6 维 + 错题 + 建议"，被引导生成自己的报告。
4. **传播归因闭环**：海报带 `ref=<原家长UID>`，新用户点击"生成你的报告"会带 `?ref=...` 进入首页（v3.7 仅记录，奖励 v3.7.1 上线）。

### 1.3 非目标
- ❌ 不做付费/订阅门控
- ❌ 不做奖励机制（v3.7.1）
- ❌ 不动 `report.html` 报告正文
- ❌ 不动 `/me/<uid>` 个人中心逻辑
- ❌ 不动 `STUDENT_ME_HTML` 已注册流程
- ❌ 不动 PDF 文件生成（仍写盘，仅不外露）

---

## 2. 架构

### 2.1 新增/修改清单

| # | 类型 | 路径/单元 | 作用 |
|---|---|---|---|
| 1 | 🆕 路由 | `GET /r/<luogu_uid>` | 报告预览中转页（公开） |
| 2 | 🆕 模板 | `REPORT_PREVIEW_HTML` | 移动优先单页（10 区） |
| 3 | 🆕 表 | `report_hides(task_id, hide_pdf, hide_html, ref_uid, created_at)` | 报告对外可见性 |
| 4 | 🆕 函数 | `_extract_ai_summary(report_md) → str` | 从第 4 节抓 AI 核心解读首段（≤200 字）|
| 5 | 🆕 函数 | `_extract_top_suggestions(report_md) → list[str]` | 从第 9 节抓 bullet（≤3 条）|
| 6 | 🆕 函数 | `_check_file_visibility(rel_path: str) → tuple[bool, str]` | 查 DB 返回 (visible, reason) |
| 7 | 🆕 函数 | `_record_hide_pdf(task_id: str)` | 报告生成时写入 `hide_pdf=1` |
| 8 | 🔧 改 | `serve_report` 路由 | 加 `_check_file_visibility` 拦截 |
| 9 | 🔧 改 | `share_card_png` 路由 | `qr_url` 改为 `/r/<uid>` |
| 10 | 🔧 改 | `STUDENT_ME_HTML` 模板 | PDF 链接灰显 + tooltip |
| 11 | 🔧 改 | `LIST_REPORTS_HTML` 模板 | report.pdf pill 灰显 |
| 12 | 🔧 改 | 报告导出流程 | 末尾调 `_record_hide_pdf(task_id)` |
| 13 | 🔧 改 | 首页 `/` | 检测 `?ref=` → 写 cookie |
| 14 | 🆕 测试 | `tests/test_report_hide_*.py` | 4 个测试文件（见 §7.1）|

### 2.2 单元边界（隔离原则）

每个单元单一职责，可独立测试：

| 单元 | 职责 | 入参 | 出参 | 依赖 |
|---|---|---|---|---|
| `_extract_ai_summary` | 解析报告 MD | str | str | 无（纯函数）|
| `_extract_top_suggestions` | 解析报告 MD | str | list[str] | 无（纯函数）|
| `_check_file_visibility` | 鉴权查询 | rel_path | (bool, str) | DB |
| `_record_hide_pdf` | 写标记 | task_id | None | DB |
| `/r/<uid>` 路由 | 组装 + 渲染 | URL | HTML | 上述 4 函数 |
| `serve_report` 路由 | 文件服务 | URL | 文件/403 | `_check_file_visibility` |
| `REPORT_PREVIEW_HTML` | 渲染 | dict | HTML | Jinja2 |

---

## 3. 数据契约

### 3.1 模板 `REPORT_PREVIEW_HTML` 入参

| 变量 | 类型 | 必填 | 缺省行为 | 来源 |
|---|---|---|---|---|
| `luogu_uid` | str | ✓ | — | URL path |
| `student_name` | str | ✗ | "UID {uid}" | **不展示真实姓名**（隐私优先）— 仅当报告 MD 头含 `# ... 选手 ...` 标题时提取 |
| `achievements` | dict | ✓ | 空 dict | `_extract_achievements_from_report` |
| `achievements.six_dim` | dict[str,int] | ✗ | `{}` | 同上（key ∈ 基础算法/数据结构/图论/动态规划/字符串/数学）|
| `achievements.ai_score_thousand` | int\|None | ✗ | `None` | 同上 |
| `achievements.ai_score_label` | str | ✗ | "—" | 同上 |
| `achievements.mistakes` | list[dict] | ✗ | `[]` | 同上 |
| `ai_summary` | str | ✗ | "" | `_extract_ai_summary` |
| `suggestions` | list[str] | ✗ | `[]` | `_extract_top_suggestions` |
| `ref` | str\|None | ✗ | `None` | URL `?ref=`，截断到 32 字符 |
| `has_report` | bool | ✓ | — | `_find_latest_report_dir` 是否非空 |

### 3.2 `report_hides` 表 DDL

```sql
CREATE TABLE IF NOT EXISTS report_hides (
    task_id     TEXT PRIMARY KEY,
    hide_pdf    INTEGER NOT NULL DEFAULT 1,   -- 1=隐藏, 0=开放
    hide_html   INTEGER NOT NULL DEFAULT 0,   -- 预留：v3.8 扩展
    ref_uid     TEXT,                        -- 预留：传播链入口
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_report_hides_ref ON report_hides(ref_uid);
```

### 3.3 `ref` 参数规范化

- 仅保留 `[A-Za-z0-9_-]`，其他字符替换为 `_`
- 最大 32 字符
- 写入 cookie `ref_uid`：30 天过期，**HttpOnly=true**（Flask 后端 `request.cookies.get("ref_uid")` 读取，**无需 JS 读**，降低 XSS 风险）

---

## 4. 页面布局

### 4.1 10 区结构

| # | 区域 | 行为 | 移动端（≤480px）|
|---|---|---|---|
| 1 | 顶部条（sticky）| Logo + 「免费生成」CTA | 固定 |
| 2 | Hero | 头像/UID + AI 分（巨字）+ GESP + 错题数 | 3 卡片堆叠 |
| 3 | AI 一句话总结 | 紫色高亮 200 字内 | 单段 |
| 4 | 6 维能力条形图 | 复用 `STUDENT_ME` 组件 | 6 行 |
| 5 | 错题本 Top 3 | 折叠"展开全部 N 道" | 折叠 |
| 6 | 核心建议 3 条 | 报告第 9 节 bullet | 3 条 |
| 7 | 双 CTA 卡片 | 「看完整」+「生成你的」| 上下堆叠 |
| 8 | 信任条 | "已为 N 位信竞家长提供" | 单行 |
| 9 | 页脚 | 关于·隐私·联系 | 3 列 |
| 10 | 底部 fixed 浮窗 | sticky CTA（移动端专属）| 隐藏 ≥md |

### 4.2 视觉规范

- 主色：`emerald-600`（个人中心一致）
- 强调色：`amber-500`（AI 分）/ `purple-500`（AI 解读）/ `red-500`（错题）
- 字体：PingFang SC / 思源黑体，移动端 16px 基线
- 容器：max-width 480px 居中

### 4.3 SEO

- `<meta name="robots" content="noindex">`（防收录）
- `<meta property="og:type" content="article">`
- `<meta property="og:title" content="{student_name} 的洛谷 AI 测评报告">`
- `<meta property="og:image" content="/me/{uid}/share-card.png">`

---

## 5. 数据流

### 5.1 报告生成时

```
task_done
  └─ export_report_files(task_id, ...)
       ├─ 写 *.md / *.html / *.pdf（4 份 PDF 仍写盘）
       └─ 🆕 _record_hide_pdf(task_id)   # INSERT hide_pdf=1
```

`_record_hide_pdf` 失败不抛异常（warning log + 不影响生成流程）。

### 5.2 用户请求报告文件时

```
GET /reports/<path>
  └─ resolve file_path
  └─ 🆕 _check_file_visibility(path)
       ├─ 解析 task_id（path 第一段目录名）
       ├─ SELECT hide_pdf, hide_html FROM report_hides WHERE task_id=?
       ├─ if *.pdf AND hide_pdf=1: return (False, "PDF 暂未开放")
       └─ if *.html AND hide_html=1: return (False, "HTML 暂未开放")
  └─ if not visible: return 403 + 提示文案
  └─ send_from_directory(...)
```

### 5.3 扫码访问中转页

```
GET /r/<luogu_uid>
  └─ latest = _find_latest_report_dir(luogu_uid)
  └─ if not latest or not (latest / "report.md").exists():
  │     return empty_state_html (status 200, 不 404)
  └─ report_md = (latest / "report.md").read_text(...)
  └─ achievements = _extract_achievements_from_report(report_md)
  └─ ai_summary = _extract_ai_summary(report_md) or ""
  └─ suggestions = _extract_top_suggestions(report_md) or []
  └─ ref = sanitize(request.args.get("ref"))
  └─ render_template_string(REPORT_PREVIEW_HTML, ...)
```

### 5.4 「生成你的报告」回流

```
中转页 CTA 点击 → /?ref=<sanitized_uid>
  └─ 首页 index() 检测 ?ref=
       ├─ 规范化 ref（截断 32 字符 / 字符白名单）
       └─ 写 cookie "ref_uid" (30 天, HttpOnly=true, SameSite=Lax)
  └─ 学员填表生成报告 → task.referral_from = ref_uid（同时读 query 和 cookie）
  └─ （v3.7 仅记录，不发奖）
```

---

## 6. 错误处理

### 6.1 错误矩阵

| 场景 | 行为 | 是否抛异常 |
|---|---|---|
| `report.md` 不存在 | 200 + 「该选手暂未生成报告」空态 | ❌ |
| 6 维全空 | 隐藏雷达，AI 分显示「—」 | ❌ |
| 错题为 0 | 「🌱 本次无错题」+ 6 维照常 | ❌ |
| `report_hides` 读失败 | 按"未隐藏"放行 + warn log | ❌ |
| `report_hides` 写失败 | warn log + 不阻塞生成 | ❌ |
| 海报 PNG 渲染失败 | hero 显示「海报暂未生成」+ 提示链接 | ❌ |
| `ref` 异常 | 截断 + 字符白名单 | ❌ |
| 中转页 DB 全挂 | 200 + 不依赖 DB 的纯渲染 | ❌ |
| 并发扫码 | Flask static cache 60s | — |

### 6.2 鉴权矩阵

| 角色 | `/r/<uid>` | `/me/<uid>` | `/reports/<uid>/*.html` | `/reports/<uid>/*.pdf` |
|---|---|---|---|---|
| 学员本人（已注册）| ✅ | ✅ | ✅ | ❌ 403 |
| 学员本人（未注册）| ✅ | ✅ 轻量版 | ✅ | ❌ |
| 陌生人扫码 | ✅ | 404/轻量版 | ✅ | ❌ 403 |
| 机器人爬虫 | 友好降级 | 404 | 友好降级 | 403 |

### 6.3 防滥用

- `/r/<uid>` 暂不接 Flask-Limiter（v3.7.1 接）
- 海报 PNG 缓存 10 min
- 不暴露学生邮箱/手机/真名
- `report.md` 直链可访问（公开是现有行为，本次不变）

---

## 7. 测试

### 7.1 单元测试

| 文件 | 测试点 |
|---|---|
| `test_extract_ai_summary.py` | 从 4 节首段抓 ≤200 字；空报告返回 ""；非中文章段容错 |
| `test_extract_suggestions.py` | 从 9 节抓 ≤3 条；空返回 []；超 3 条截断 |
| `test_check_visibility.py` | 命中 hide_pdf=1 ⇒ False；未命中 ⇒ True；DB 异常 ⇒ True（fail-open）|
| `test_record_hide_pdf.py` | INSERT 成功可回读；DB 异常不抛 |

### 7.2 路由级

```python
def test_preview_existing_report():       # /r/e279a542 → 200, 含 AI 分+免费生成
def test_preview_no_report():             # /r/9999999 → 200, 空态
def test_pdf_blocked():                   # /reports/<tid>/report.pdf → 403
def test_html_allowed_when_pdf_hidden():  # /reports/<tid>/report.html → 200
def test_me_page_pill_gray():            # /me/<uid> 含 "PDF 暂未开放"
def test_qr_url_updated():               # 海报 _render_share_card_png 传入 qr_url 含 /r/
def test_ref_sanitization():              # /r/<uid>?ref=<evil> 不爆, 截断到 32 字符
def test_ref_cookie_written():            # /?ref=xxx 响应含 Set-Cookie
```

### 7.3 验收清单

- [ ] `report_hides` 表创建（幂等迁移）
- [ ] 生成流程写 `hide_pdf=1`
- [ ] `/reports/<path>` 拦截 PDF
- [ ] `/r/<uid>` 路由上线
- [ ] `REPORT_PREVIEW_HTML` 模板完成
- [ ] 海报 QR URL = `/r/<uid>`
- [ ] `/me/<uid>` PDF 链接灰显
- [ ] 列表页 report.pdf pill 灰显
- [ ] 单元 + 路由测试全过
- [ ] 移动端 4 屏截图验收
- [ ] 灰度：1 个新报告 → 验证 → 全量

---

## 8. 范围声明

### 8.1 v3.7（本 spec）
✅ 全部 14 项改动 + 测试

### 8.2 v3.7.1（不在本 spec）
- 邀请奖励机制（家长推荐 N 人 → 订阅/证书/咨询券）
- Flask-Limiter 接入
- 完整版报告内部的"生成你的报告" CTA

### 8.3 v3.8+（不在本 spec）
- 白牌 API（培训机构嵌报告）
- 省级榜单
- GESP 复盘日

---

## 9. 风险与回滚

| 风险 | 概率 | 缓解 | 回滚 |
|---|---|---|---|
| 海报 QR 改动后老海报失效 | 低 | 兼容期：扫 `/r/<uid>` 找不到 fallback 到 `/me/<uid>` | 改回 `qr_url = /me/<uid>` |
| `report_hides` 表读写阻塞 | 极低 | fail-open + warn log | DROP 表（不破坏主流程）|
| `ref` 被滥用 | 中 | 截断 + 字符白名单 | 关闭 cookie 写入 |
| 移动端样式不达预期 | 中 | Tailwind CDN + iPhone 12 模拟 | 调整 breakpoint |

**回滚总开关**：1 个 SQLite UPDATE 把 `hide_pdf=0` 全部 PDF 立即恢复。

---

## 10. 设计决策记录

| 决策 | 选项 | 选择 | 理由 |
|---|---|---|---|
| 扫码落地 | `/me` / `/reports/<uid>.html` / 新建中转页 / 首页 | **新建中转页** | 用户选；平衡信息密度与转化漏斗 |
| PDF 物理删除 | 删盘 / 留盘 + DB 标记 | **留盘 + 标记** | 保留生成成本，可控重开 |
| 隐藏粒度 | 报告级 / 文件级 | **报告级** | 1 task 出 4 PDF，统一开关更简单 |
| ref 注入点 | query / cookie / header | **query + cookie** | 落地页和首页都能用 |
| 奖励机制 | v3.7 / v3.7.1 | **v3.7.1** | YAGNI：先把传播跑通 |

---

**Spec 完成 · 待用户审查。**
