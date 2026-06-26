"""
v3.9.74 · VJudge 公开数据抓取 + 解析 + 后台 worker
v3.10.0.4 · 深度抓取(fetch_vjudge_solved_playwright):用 Playwright 渲染 #solved 拿 OJ 分布 + 题目 ID

抓取目标: 主页(user/<u>) 拿 AC / 提交 / OJ 分布 + #solved 页拿题目列表
限速 3s/req, 失败 3 次指数退避, 缓存原始 HTML 到 .source_cache/vjudge/<u>/<page>.html
"""

# === 基础依赖 ===
import json
import os
import re
import time
import threading
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


# === 兼容性占位(原 docstring 描述保留在此) ===
# 设计要点(吸取 AtCoder 经验):
#   - 全公开页面抓取,无需 cookie/login
#   - 限速 3s/req(比 AtCoder 严)
#   - 失败 3 次指数退避重试
#   - 缓存原始 HTML 到 .source_cache/vjudge/<username>/<page>.html
#   - 解析器纯函数 + 容错(字段缺失返回 None 不崩)
#
# 抓取目标:
#   1) 主页: https://vjudge.net/user/<username>
#      - nick(用户昵称)
#      - 注册时间
#      - 总提交数 / AC / WA / TLE / RE / CE
#      - 各 OJ 解决题数分布
#   2) 已解决题列表: https://vjudge.net/user/<username># solved
#      - 题号、题名、AC 时间、来源 OJ
#      - 单次最多 200 条
#
# 风控:
#   - 限速: 全局 token bucket,1 req / 3s
#   - 限流响应(429): 退避 60s 再重试
#   - 用户不存在(404): 直接拒,不重试
#   - 解析失败: 不影响其他字段,容错后继续
#
# 对外接口:
#   - fetch_vjudge_profile(username) -> dict   单次抓取(主页+已解决列表)
#   - parse_vjudge_user_page(html) -> dict    主页 HTML 解析
#   - parse_vjudge_solved_page(html) -> list  已解决题 HTML 解析
#   - start_vjudge_worker()                   启动后台 worker(单线程轮询)


# ---- 全局限速锁(3s/req,VJudge 比 AtCoder 严)----
import threading as _th
_ip_lock = _th.Lock()
_last_request_at: float = 0.0
_MIN_INTERVAL = 3.0  # 3 秒


class ParseError(Exception):
    """v3.9.74 · 解析失败(HTML 结构变化/字段缺失)。"""


class FetchError(Exception):
    """v3.9.74 · 抓取失败(HTTP 错误/超时/限流)。

    status: HTTP 状态码(可能为 None)
    """

    def __init__(self, msg: str, status: Optional[int] = None):
        super().__init__(msg)
        self.status = status


# ---- username 合法性 ----
import re as _re
_USERNAME_RE = _re.compile(r"^[A-Za-z0-9_\-]{3,30}$")


def is_username_valid(username: str) -> bool:
    return bool(username) and bool(_USERNAME_RE.match(username))


# ---- 工具函数 ----
def _extract_text(elem) -> str:
    if elem is None:
        return ""
    return elem.get_text(" ", strip=True)


def _clean_int(s: str) -> int:
    if not s:
        return 0
    s = s.replace(",", "").strip()
    m = _re.search(r"-?\d+", s)
    return int(m.group(0)) if m else 0


def _clean_float(s: str) -> float:
    if not s:
        return 0.0
    s = s.replace("%", "").replace(",", "").strip()
    m = _re.search(r"\d+(\.\d+)?", s)
    if not m:
        return 0.0
    return float(m.group(0))


# ---- 深度抓取:Playwright 渲染 #solved 拿 OJ 分布 + 题目 ID ----
def fetch_vjudge_solved_playwright(username: str, timeout_sec: int = 25) -> list[dict]:
    """v3.10.0.4 · 用 Playwright headless 渲染 vjudge.net/user/<u>#solved。

    返回 list[dict],每条:
        { oj: str, problem_id: str, problem_url: str }

    失败/Playwright 不可用 → 返回 []
    5-10s/学员,作为"深度抓取"按钮的后端。
    """
    import re
    try:
        from playwright.sync_api import sync_playwright
    except Exception as _e:
        logger.warning(f"[v3.10.0.4] playwright not installed: {_e}")
        return []

    out: list[dict] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                page = browser.new_page()
                # v3.10.0.4 fix: VJudge 是 SPA(WebSocket 长连),networkidle 永远超时
                # 改 domcontentloaded,再用显式选择器等待
                try:
                    page.goto(
                        f"https://vjudge.net/user/{username}#solved",
                        wait_until="domcontentloaded", timeout=timeout_sec * 1000
                    )
                except Exception as _ge:
                    # goto 超时也继续:可能 DOM 已经部分加载
                    logger.info(f"[v3.10.0.4] goto timeout for {username}, continue anyway: {_ge!r}")
                # 给客户端 JS 3s 跑完 (VJudge 改版后 SPA 加载要 ~2s)
                try:
                    page.wait_for_timeout(3000)
                except Exception:
                    pass
                # 抓所有 problem 链接 (从主页 #solved tab 或 #status tab 都行)
                try:
                    links = page.evaluate("""
                      () => Array.from(document.querySelectorAll("a"))
                        .filter(a => {
                          var h = a.getAttribute("href") || "";
                          return h.includes("/status#un=") && h.includes("probNum=");
                        })
                        .map(a => ({
                          href: a.getAttribute("href"),
                          text: (a.innerText || "").trim()
                        }))
                    """)
                except Exception as _ee:
                    logger.warning(f"[v3.10.0.4] page.evaluate failed for {username}: {_ee!r}")
                    links = []
                for l in links or []:
                    href = l.get("href") or ""
                    text = (l.get("text") or "").strip()
                    if not href or not text:
                        continue
                    # 解析 OJId=... 和 probNum=... (URL 编码)
                    m_oj = _re.search(r"OJId=([^&]+)", href)
                    m_pn = _re.search(r"probNum=([^&]+)", href)
                    if not m_oj or not m_pn:
                        continue
                    from urllib.parse import unquote
                    oj = unquote(m_oj.group(1))
                    pn = unquote(m_pn.group(1))
                    out.append({
                        "oj": oj,
                        "problem_id": pn,
                        "problem_url": f"https://vjudge.net{href}" if href.startswith("/") else href,
                    })
            finally:
                browser.close()
    except Exception as _e:
        logger.warning(f"[v3.10.0.4] playwright fetch failed for {username}: {_e!r}")
    # 去重
    seen = set()
    uniq = []
    for item in out:
        k = (item["oj"], item["problem_id"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(item)
    return uniq


# ---- 解析器: VJudge 主页 ----
def parse_vjudge_user_page(html: str) -> dict:
    """v3.10.0.4 · 解析 VJudge 用户主页。

    返回 dict:
      nick: str
      register_time: ISO 字符串(可能空)
      total_submissions: int
      total_ac: int
      total_wa: int
      total_tle: int
      total_re: int
      total_ce: int
      ac_rate: 0~1 浮点
      oj_stats: { "Codeforces": 10, "AtCoder": 8, ... }

    v3.10.0.4 · VJudge 改版:核心统计从 HTML 表格挪到
        <script type="application/json" id="profile-header-data">
    原表格靠 JS 渲染,server-side HTML 只剩空表壳,所以只读 JSON。
    """
    import json as _json
    soup = BeautifulSoup(html, "html.parser")

    # 1. nick: <h3>Username</h3>
    nick = ""
    h3 = soup.find("h3")
    if h3:
        text = h3.find(string=lambda s: not getattr(s.parent, "name", "") == "small")
        if text:
            nick = text.strip()

    # 2. 核心数据:JSON blob
    register_time = ""
    oj_stats: dict[str, int] = {}
    total_submissions = total_ac = total_wa = total_tle = total_re = total_ce = 0
    ac_rate = 0.0

    data_script = soup.find("script", id="profile-header-data")
    if data_script:
        try:
            data = _json.loads(data_script.string or "{}")
            counts = (data.get("counts") or {}) if isinstance(data, dict) else {}
            # acAll=总AC; attAll=总尝试
            total_ac = int(counts.get("acAll", 0) or 0)
            total_submissions = int(counts.get("attAll", 0) or 0)
            # WA/TLE/RE/CE:新版本不再单独暴露(服务端只给总数 + AC)
            total_wa = 0
            total_tle = 0
            total_re = 0
            total_ce = 0
            if total_submissions > 0:
                ac_rate = round(total_ac / total_submissions, 4)
            # ranks 可能含总排名,先记下来
            # (暂不存,避免 schema 变更)
        except Exception as e:
            logger.warning(f"[v3.10.0.4] parse profile-header-data failed: {e}")

    # 3. 兜底:扫 <table> 找 OJ 分布(VJudge 部分老样式可能还在)
    #    新版 HTML 表格是空壳,跳也无所谓
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for tr in rows:
            cells = [_extract_text(td) for td in tr.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            oj_link = tr.find("a", href=_re.compile(r"^/user/"))
            if oj_link:
                oj = _extract_text(oj_link)
                if oj and oj.lower() not in ("status", "submit"):
                    try:
                        cnt = _clean_int(cells[-1])
                        if cnt > 0:
                            oj_stats[oj] = oj_stats.get(oj, 0) + cnt
                    except Exception:
                        pass

    # 注册时间:新版是 "11 months ago" 相对描述,无法直接拿 ISO
    # 兜底从 <span title="..."> 找日期格式
    span = soup.find("span", title=_re.compile(r"\d{4}-\d{2}-\d{2}"))
    if span:
        register_time = span.get("title", "")

    return {
        "nick": nick,
        "register_time": register_time,
        "total_submissions": total_submissions,
        "total_ac": total_ac,
        "total_wa": total_wa,
        "total_tle": total_tle,
        "total_re": total_re,
        "total_ce": total_ce,
        "ac_rate": ac_rate,
        "oj_stats": oj_stats,
    }


# ---- 解析器: VJudge 已解决题列表 ----
def parse_vjudge_solved_page(html: str, limit: int = 200) -> list[dict]:
    """v3.9.74 · 解析 VJudge 已解决题列表。

    返回 list[dict]:
      oj, problem_id, title, ac_time

    VJudge 的"已解决"页是分页 / 加载的,这里只解析静态 HTML 中
    可见的题目,通常前 20 条是可靠的。
    """
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []

    # 找到 #solvedPanel 表格(优先)
    panel = soup.find(id=_re.compile(r"solved|solvedTable"))
    table = panel if panel and getattr(panel, "name", None) == "table" else (
        panel.find("table") if panel else None
    )
    if not table:
        # 兜底: 找所有 table,选含"Problem"列的那张
        for t in soup.find_all("table"):
            if "Problem" in t.get_text() or "problem" in (t.get("id") or ""):
                table = t
                break
    if not table:
        return results

    rows = table.find_all("tr")
    for tr in rows[1:]:  # 跳过表头
        cells = tr.find_all("td")
        if len(cells) < 3:
            continue
        # OJ 来源: 第一个 cell 一般是 <a href="/problem/Codeforces/1234A"> 里的
        oj = ""
        a = tr.find("a", href=_re.compile(r"/problem/"))
        if a:
            href = a.get("href", "")
            m = _re.match(r"/problem/([^/]+)/", href)
            if m:
                oj = m.group(1)
        # 题目 ID + 标题
        problem_id = ""
        title = ""
        for td in cells:
            text = _extract_text(td)
            a2 = td.find("a")
            if a2 and a2.get("href", "").startswith("/problem/"):
                href = a2.get("href", "")
                m = _re.search(r"/problem/[^/]+/(.+)$", href)
                if m:
                    problem_id = unescape(m.group(1))
                title = text
                break
        # AC 时间: 通常是最后一列(或带 title 属性的 td)
        ac_time = ""
        for td in cells:
            t = td.get("title", "")
            if t and _re.search(r"\d{4}-\d{2}-\d{2}", t):
                ac_time = t
                break
        if not ac_time and cells:
            # 找最后非空文本
            for td in reversed(cells):
                t = _extract_text(td)
                if t and _re.search(r"\d{4}-\d{2}-\d{2}", t):
                    ac_time = t
                    break
        if problem_id:
            results.append({
                "oj": oj or "Unknown",
                "problem_id": problem_id,
                "title": title or problem_id,
                "ac_time": ac_time or None,
            })
        if len(results) >= limit:
            break

    return results


# ---- HTTP 抓取 ----
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_BASE_URL = "https://vjudge.net"


def _ip_throttle() -> None:
    """全局 IP 限速(VJudge 比 AtCoder 严,3s/req)。"""
    global _last_request_at
    with _ip_lock:
        now = time.time()
        wait = _MIN_INTERVAL - (now - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.time()


def _httpx_get_with_retry(url: str, max_retries: int = 3) -> tuple[int, str]:
    """v3.9.74 · GET with retry。

    返回 (status, html) 或 raise FetchError。
    """
    last_exc: Optional[Exception] = None
    backoff = 5
    for attempt in range(1, max_retries + 1):
        _ip_throttle()
        try:
            with httpx.Client(
                timeout=httpx.Timeout(20.0, connect=10.0),
                follow_redirects=True,
                headers={"User-Agent": _USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            ) as client:
                resp = client.get(url)
            status = resp.status_code
            if status == 200:
                return 200, resp.text
            if status == 404:
                # 用户不存在,直接 raise(不重试)
                raise FetchError(f"VJudge 404: {url}", status=404)
            if status == 429:
                # 限流,等 60s 再试
                logger.warning(f"[vjudge] 429 rate limited, sleep 60s, attempt={attempt}")
                time.sleep(60)
                continue
            if status == 403:
                # IP 被人机验证拦了
                logger.warning(f"[vjudge] 403 forbidden (likely captcha), attempt={attempt}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            # 其它 4xx/5xx: 重试
            logger.warning(f"[vjudge] HTTP {status}, attempt={attempt}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            last_exc = FetchError(f"HTTP {status}: {url}", status=status)
        except FetchError:
            raise
        except Exception as e:
            logger.warning(f"[vjudge] network error {e!r}, attempt={attempt}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            last_exc = e
    raise FetchError(f"fetch failed after {max_retries} attempts: {url}") from last_exc


def _save_raw_html(cache_dir: Path, username: str, page: str, html: str) -> str:
    """v3.9.74 · 缓存原始 HTML,返回相对路径。"""
    try:
        d = cache_dir / username
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{page}.html"
        p.write_text(html, encoding="utf-8")
        return str(p.relative_to(cache_dir.parent.parent))
    except Exception as e:
        logger.warning(f"[vjudge] save cache failed: {e}")
        return ""


# ---- 主入口 ----
def fetch_vjudge_profile(username: str, cache_dir: Optional[Path] = None,
                        progress_cb: Optional[callable] = None) -> dict:
    """v3.9.74 · 抓取 + 解析 + 缓存(三步一体)。

    返回 dict,字段同 vjudge_persist_data 的 raw schema。
    raise FetchError(404) 如果用户不存在。

    v3.10.0.4 · progress_cb(step, total, msg) 用于实时更新进度
    """
    if not is_username_valid(username):
        raise FetchError(f"username 格式不合法: {username}")

    cache_dir = cache_dir or (Path(__file__).resolve().parent / ".source_cache" / "vjudge")

    def _step(s: int, t: int, m: str) -> None:
        # v3.10.0.13 · 进度用百分比(0-100)而非旧的 1-3,
        # 让前端进度条平滑推进,不卡 30s
        if progress_cb:
            try:
                progress_cb(s, t, m)
            except Exception:
                pass

    # 1. 抓主页
    _step(20, 100, f"📥 下载 {username} 的 profile.html (vjudge.net)…")
    user_url = f"{_BASE_URL}/user/{username}"
    try:
        status, html = _httpx_get_with_retry(user_url)
    except FetchError as e:
        if e.status == 404:
            raise FetchError(f"VJudge 用户 {username} 不存在", status=404) from e
        raise
    _save_raw_html(cache_dir, username, "profile", html)
    _step(45, 100, "📊 解析 profile.html(AC/WA/OJ 分布)…")
    profile = parse_vjudge_user_page(html)

    # 2. 抓已解决题列表(失败也不阻塞,只记日志)
    solved: list[dict] = []
    try:
        _step(65, 100, f"📥 下载 {username} 的 solved 列表…")
        solved_url = f"{user_url}#solved"
        _, solved_html = _httpx_get_with_retry(solved_url)
        _save_raw_html(cache_dir, username, "solved", solved_html)
        solved = parse_vjudge_solved_page(solved_html, limit=200)
        _step(85, 100, f"📊 解析 solved 列表(共 {len(solved)} 题)…")
    except Exception as e:
        logger.warning(f"[vjudge] solved list fetch failed for {username}: {e}")
        _step(85, 100, f"⚠️ solved 列表抓取失败,使用 profile 兜底数据…")

    # 3. 汇总
    _step(95, 100, "💾 写入数据库…")
    # v3.10.0.4 · 兜底:静态 HTML 拿不到题目列表(Angular SPA),
    # 但内嵌 JSON 的 counts.acAll 是真实 AC 题数(总 AC = 已解决数)。
    # 用它做最小展示,前端能看到"已解决 N 题"。
    if not solved:
        solved_count = int(profile.get("total_ac") or 0)
    else:
        solved_count = len(solved)
    raw = {
        **profile,
        "solved_count": solved_count,
        "solved_list": solved,
    }
    _step(98, 100, f"✅ 完成: AC {raw.get('total_ac', 0)} / 解决 {solved_count} 题")
    return raw


# ---- 后台 worker ----
_worker_thread: Optional[threading.Thread] = None
_worker_lock = threading.Lock()


def start_vjudge_worker() -> None:
    """v3.9.74 · 启动后台 worker 线程(单实例,daemon)。

    web_app 启动时调一次。worker 每 2s 轮询 student_vjudge_fetch_tasks 表。
    """
    with _worker_lock:
        global _worker_thread
        if _worker_thread and _worker_thread.is_alive():
            return
        # v3.10.0 · 启动时回收卡死任务:
        # 1) status='fetching' 但 started_at 超过 10 分钟 → 置为 failed(说明 worker 进程死了/被 kill)
        # 2) status='pending' 但 created_at 超过 1 小时 → 置为 failed(节流/重试已无意义)
        # 避免之前测试/异常退出留下永远"抓取中"的僵尸任务占位
        try:
            from task_store import _get_conn as _vj_conn_factory
            _c = _vj_conn_factory()
            try:
                _c.execute("""
                    UPDATE student_vjudge_fetch_tasks
                    SET status='failed',
                        error_msg='worker 进程已退出,任务超时回收',
                        finished_at=CURRENT_TIMESTAMP
                    WHERE status='fetching'
                      AND started_at IS NOT NULL
                      AND (strftime('%s','now') - strftime('%s', started_at)) > 600
                """)
                _c.execute("""
                    UPDATE student_vjudge_fetch_tasks
                    SET status='failed',
                        error_msg='排队超过 1 小时,自动放弃',
                        finished_at=CURRENT_TIMESTAMP
                    WHERE status='pending'
                      AND (strftime('%s','now') - strftime('%s', created_at)) > 3600
                """)
                _c.commit()
            finally:
                _c.close()
        except Exception as _e:
            logger.debug(f"[v3.9.74] vjudge task recovery skipped: {_e}")
        _worker_thread = threading.Thread(
            target=_vjudge_worker_loop, name="vjudge-worker", daemon=True
        )
        _worker_thread.start()
        logger.info("[v3.9.74] vjudge worker started")


def _vjudge_worker_loop() -> None:
    """v3.9.74 · worker 主循环:轮询 pending 任务,跑 fetch+persist。

    v3.10.0.4-fix · 每 10 轮做一次"卡住任务"清理:
      - status='fetching' 但 started_at > 90s(比 HTTP 超时稍长)
        → 置为 failed,前端能看到"被限流/超时",而不是永远"准备中"
      - status='pending' 但 created_at > 10 分钟 → 同样置 failed
    """
    from task_store import (
        vjudge_pickup_pending_task,
        vjudge_finish_task,
        vjudge_persist_data,
        vjudge_mark_failed,
        vjudge_update_progress,  # v3.10.0.4
    )
    _idle_counter = 0  # v3.10.0.4-fix · 闲置计数,达到阈值做卡住清理
    while True:
        try:
            # v3.10.0.4-fix · 每 5 次闲置轮询(≈10s)做一次卡住任务清理
            if _idle_counter > 0 and _idle_counter % 5 == 0:
                try:
                    from task_store import _get_conn as _vj_recover_conn, vjudge_mark_failed as _vj_mark_failed
                    _rc = _vj_recover_conn()
                    try:
                        # v3.10.0.4-fix · 卡住 90s 的任务 → 失败回收
                        # 同时把 student_vjudge_data.link_status 也置为 failed,
                        # 否则前端 get_vjudge_context 仍返回 'pending/fetching',卡死 UI
                        stuck_rows = _rc.execute("""
                            SELECT task_id, student_id, error_msg
                            FROM student_vjudge_fetch_tasks
                            WHERE status='fetching'
                              AND started_at IS NOT NULL
                              AND (strftime('%s','now') - strftime('%s', started_at)) > 90
                        """).fetchall()
                        cur1 = _rc.execute("""
                            UPDATE student_vjudge_fetch_tasks
                            SET status='failed',
                                error_msg='worker 卡住超过 90 秒,自动回收(可能被 VJudge 限流)',
                                finished_at=CURRENT_TIMESTAMP,
                                progress_msg='⚠️ 抓取超时(>90s),请稍后重试或换轻量模式'
                            WHERE status='fetching'
                              AND started_at IS NOT NULL
                              AND (strftime('%s','now') - strftime('%s', started_at)) > 90
                        """)
                        cur2 = _rc.execute("""
                            UPDATE student_vjudge_fetch_tasks
                            SET status='failed',
                                error_msg='排队超过 10 分钟,自动放弃',
                                finished_at=CURRENT_TIMESTAMP
                            WHERE status='pending'
                              AND (strftime('%s','now') - strftime('%s', created_at)) > 600
                        """)
                        _rc.commit()
                    finally:
                        _rc.close()
                    # v3.10.0.4-fix · 同步 student_vjudge_data.link_status,
                    # 这样下次 get_vjudge_context 才会返回 'failed' → 前端跳出"卡死"
                    for r in (stuck_rows or []):
                        try:
                            _vj_mark_failed(
                                r["student_id"],
                                "worker 卡住超过 90 秒,自动回收",
                                status="failed",
                            )
                        except Exception as _me:
                            logger.debug(f"[v3.10.0.4-fix] mark failed skip: {_me}")
                    if cur1.rowcount or cur2.rowcount:
                        logger.warning(
                            f"[v3.10.0.4-fix] vjudge recovery: fetching→failed={cur1.rowcount}, "
                            f"pending→failed={cur2.rowcount}"
                        )
                except Exception as _re:
                    logger.debug(f"[v3.10.0.4-fix] vjudge recovery skip: {_re}")
            task = vjudge_pickup_pending_task()
            if not task:
                _idle_counter += 1
                time.sleep(2.0)
                continue
            _idle_counter = 0  # 有活干,重置闲置计数
            task_id = task["task_id"]
            student_id = task["student_id"]
            username = task["username"]
            trigger = task.get("trigger", "user_link")
            logger.info(f"[v3.9.74] vjudge worker picked up: {task_id} user={username} trigger={trigger}")
            # v3.10.0.4 · 进度回调 → 写库,前端 2s 轮询可看到
            def _cb(step: int, total: int, msg: str) -> None:
                try:
                    vjudge_update_progress(task_id, step, total, msg)
                except Exception as _pe:
                    logger.debug(f"[v3.10.0.4] progress update fail: {_pe}")
            try:
                # v3.10.0.13 · pickup 后立即推 15/100,让前端看到"worker 已拾取"
                _cb(15, 100, f"🚀 worker 已拾取任务,开始抓 {username}…")
                raw = fetch_vjudge_profile(username, progress_cb=_cb)
                # v3.10.0.4 · heavy 模式:再 Playwright 抓 OJ 分布 + 题目 ID,合并到 solved_list
                if trigger == "user_refresh_heavy":
                    _cb(90, 100, "🎭 Playwright 渲染 #solved 拿 OJ 分布 + 题目…")
                    heavy = fetch_vjudge_solved_playwright(username, timeout_sec=20)
                    if heavy:
                        # 合并(heavy 优先,有 title/url/oj)
                        exist_ids = {(p.get("oj"), p.get("problem_id")) for p in raw.get("solved_list", [])}
                        merged = list(raw.get("solved_list", [])
)
                        for p in heavy:
                            k = (p.get("oj"), p.get("problem_id"))
                            if k in exist_ids:
                                continue
                            merged.append(p)
                        raw["solved_list"] = merged
                        raw["solved_count"] = len(merged)
                        # oj_stats 也合并
                        oj_stats = dict(raw.get("oj_stats") or {})
                        for p in heavy:
                            o = p.get("oj") or "Unknown"
                            oj_stats[o] = oj_stats.get(o, 0) + 1
                        raw["oj_stats"] = oj_stats
                        logger.info(f"[v3.10.0.4] heavy fetch added {len(heavy)} problems for {username}")
                _cb(100, 100, f"✅ 全部完成:AC {raw.get('total_ac', 0)} / 解决 {raw.get('solved_count', 0)} 题")
                vjudge_persist_data(student_id, username, raw)
                vjudge_finish_task(task_id, "succeeded")
                logger.info(f"[v3.9.74] vjudge worker done: {task_id}")
            except FetchError as e:
                status = "rate_limited" if e.status == 429 else "failed"
                vjudge_mark_failed(student_id, str(e), status=status)
                vjudge_finish_task(task_id, status, error_msg=str(e))
                logger.warning(
                    f"[v3.9.74] vjudge worker failed: {task_id} {status} {e}"
                )
            except Exception as e:
                vjudge_mark_failed(student_id, f"exception: {e!r}", status="failed")
                vjudge_finish_task(task_id, "failed", error_msg=str(e))
                logger.exception(f"[v3.9.74] vjudge worker exception: {task_id}")
        except Exception as loop_exc:
            # v3.9.74 · 表还没初始化(如测试场景)就安静等 10s 再试,别刷屏
            err_str = str(loop_exc)
            if "no such table" in err_str or "unable to open" in err_str:
                time.sleep(10.0)
                continue
            logger.exception(f"[v3.9.74] vjudge worker loop error: {loop_exc}")
            time.sleep(5.0)


# ---- smoke ----
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python vjudge_fetcher.py <username>")
        sys.exit(1)
    u = sys.argv[1]
    logging.basicConfig(level=logging.INFO)
    try:
        result = fetch_vjudge_profile(u)
        import json
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except FetchError as e:
        print(f"FETCH ERROR: {e}", file=sys.stderr)
        sys.exit(2)
