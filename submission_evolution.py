# -*- coding: utf-8 -*-
"""v3.9.39 提交代码考古（submission_evolution.py）

对「多次提交但未一次 AC」的题目，逐版拉取源代码、做 diff，挖掘：
  1. 每次提交改进了什么（diff 时间线）
  2. 思维漏洞分类（按错误模式）
  3. 未被发现的根因（深挖卡题）
  4. 学习建议（书 / 题单 / 训练）

设计原则：
  - API 预算可控：TOP 5 题 × 每题最多 5 版 = 25 次 get_record，
    4 并发 ≈ 10s；落盘 .source_cache 复用
  - 即使 luogu 拒绝服务（429/403），也降级到「只有 sourceCodeLength 长度差」的启发式
  - 输出已为 AI prompt 做好分块（避免一次性喂几万 token 源码）
"""
from __future__ import annotations

import difflib
import json
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# v3.9.39 · 与 web_app.py / behavior_analyzer.py 同款北京时区
_BJ_TZ = timezone(timedelta(hours=8))

# 洛谷提交状态码
_STATUS_LABELS = {
    0: "Waiting",
    1: "Judging",
    2: "Compile Error",  # CE
    3: "System Error",
    4: "Judging Failed",  # 评测失败（CE 类）
    5: "Compile Error",   # 部分版本是 5 = CE
    6: "Compile Error",
    7: "Output Limit Exceeded",
    8: "Time Limit Exceeded",  # TLE
    9: "Wrong Answer",  # WA
    10: "Runtime Error",  # RE
    11: "Judging",
    12: "Accepted",  # AC
    13: "Compile Error",  # CE
    14: "Compile Error",  # CE
}
# 简化版（用于展示）
_STATUS_SHORT = {
    2: "CE", 4: "CE", 5: "CE", 6: "CE", 13: "CE", 14: "CE",
    7: "OLE", 8: "TLE", 9: "WA", 10: "RE", 12: "AC",
}

# 每版代码最多取多少字符喂给 AI（避免 prompt 爆炸）
_CODE_SNIPPET_HEAD = 280
_CODE_SNIPPET_TAIL = 120
_DIFF_CONTEXT = 60  # difflib 输出时只显示变化行 ± N 行的 context


def _version_cache_dir(uid: int | str, pid: str) -> Path:
    """v3.9.39 · 每道题的所有版本源码缓存目录

    .source_cache/<uid>/<pid>/versions/<record_id>.json
    """
    safe_pid = str(pid).replace("/", "_").replace(" ", "_")
    return Path(__file__).resolve().parent / ".source_cache" / str(uid) / safe_pid / "versions"


def _load_cached_version(uid: int | str, pid: str, record_id: int) -> dict | None:
    f = _version_cache_dir(uid, pid) / f"{record_id}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_cached_version(uid: int | str, pid: str, record_id: int, record: dict) -> None:
    if not record or not record.get("sourceCode"):
        return
    d = _version_cache_dir(uid, pid)
    d.mkdir(parents=True, exist_ok=True)
    record = dict(record)
    record["_cached_at"] = datetime.now(_BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
    (d / f"{record_id}.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )


def _fetch_one_version(luogu, uid: int, pid: str, record_id: int) -> dict:
    """抓取单条记录的源码（带缓存 + 错误降级）"""
    cached = _load_cached_version(uid, pid, record_id)
    if cached and cached.get("sourceCode"):
        return {**cached, "_from_cache": True}
    try:
        # 限速（与 export_for_ai.py 的 _pick_record_for_problem 同款）
        time.sleep(0.25)
        resp = luogu.get_record(str(record_id))
        rec = resp.to_json() if hasattr(resp, "to_json") else dict(resp)
    except Exception as e:
        return {"_error": str(e), "id": record_id, "pid": pid}
    if rec and rec.get("sourceCode"):
        _save_cached_version(uid, pid, record_id, rec)
    return rec or {"_error": "empty", "id": record_id, "pid": pid}


def _status_label(status: int) -> str:
    return _STATUS_SHORT.get(status, str(status))


def _evolution_score(recs: list[dict]) -> float:
    """v3.9.39 · 一道题的"考古价值"分（v3.9.39.1 重构）

    设计目标：让"状态变化丰富"的题（能体现思维演化）排前面。
    用户诉求："挖掘学生有哪些思维漏洞，每次提交改进了什么"——这本质上是
    状态变迁驱动的，因此把"相邻版本状态变化次数"作为核心奖励项。

    维度（从高到低优先级）：
      + 状态变化次数 × 2.0        【核心】变化 = 做了不同尝试 = 有 diff 价值
      + 提交次数 × 0.5            【基础】越多越好
      + 最终 AC 且提交 ≥ 3 次     【+4】说明学生多次迭代后 AC，是典型思维演化
      + 最终未 AC                 【+3】说明卡题，更需要"挖掘根因"
      + 含 WA / TLE / RE         【各 +1】非简单错误
      + 状态码 distinct 数量 × 0.5【多样性】
      - 全 CE                     【-3】没分析价值
    """
    n = len(recs)
    if n < 2:
        return 0
    statuses = [r.get("status", 0) for r in recs]
    final = statuses[-1]
    is_ac = (final == 12)
    distinct = set(statuses)
    # 核心：相邻版本状态变化次数
    status_changes = sum(1 for i in range(1, n) if statuses[i] != statuses[i - 1])
    score = n * 0.5 + status_changes * 2.0
    if is_ac and n >= 3:
        score += 4  # 多次迭代后 AC，最典型的思维演化
    if not is_ac:
        score += 3  # 未 AC，卡题，需要挖根因
    if 9 in distinct:  # WA
        score += 1
    if 8 in distinct:  # TLE
        score += 1
    if 10 in distinct:  # RE
        score += 1
    score += 0.5 * len(distinct)
    if all(s in {2, 4, 5, 6, 13, 14} for s in statuses):
        score -= 3  # 全是 CE，没分析价值
    return score


def _short_diff(prev: str, curr: str) -> str:
    """v3.9.39 · 紧凑版 diff（≤ 200 字符），让 AI 看到具体改了什么

    输出形如：
      - [- 3 行：删了读入优化]
      - [+ 8 行：新增特判 n=1]
    """
    if not prev or not curr:
        return "（首版/末版缺源码）"
    if prev == curr:
        return "（与上一版完全一致）"
    prev_lines = prev.splitlines()
    curr_lines = curr.splitlines()
    sm = difflib.SequenceMatcher(a=prev_lines, b=curr_lines, autojunk=False)
    additions, deletions, replaces = [], [], 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        a = "\n".join(prev_lines[i1:i2])
        b = "\n".join(curr_lines[j1:j2])
        if tag == "replace":
            replaces += 1
            a_short = a[:80].replace("\n", " ⏎ ")
            b_short = b[:80].replace("\n", " ⏎ ")
            deletions.append(f"删 {i1 + 1}-{i2}: {a_short}")
            additions.append(f"加 {j1 + 1}-{j2}: {b_short}")
        elif tag == "delete":
            deletions.append(f"删 {i1 + 1}-{i2}: {a[:120].replace(chr(10), ' ⏎ ')}")
        elif tag == "insert":
            additions.append(f"加 {j1 + 1}-{j2}: {b[:120].replace(chr(10), ' ⏎ ')}")
    parts = []
    if deletions:
        parts.append("  " + "; ".join(deletions[:2]))
    if additions:
        parts.append("  " + "; ".join(additions[:2]))
    summary = "换" if replaces else ("净删" if len(deletions) > len(additions) else "净加")
    parts.insert(0, f"（{summary} | 删 {len(deletions)} 处 / 加 {len(additions)} 处）")
    return "\n".join(parts)[:400]


def _build_version_payload(rec: dict, version_no: int) -> dict:
    """v3.9.39 · 序列化单个版本（喂给 AI 用）"""
    code = rec.get("sourceCode") or ""
    head = code[:_CODE_SNIPPET_HEAD]
    tail = code[-_CODE_SNIPPET_TAIL:] if len(code) > _CODE_SNIPPET_HEAD + _CODE_SNIPPET_TAIL else ""
    return {
        "v": version_no,
        "record_id": rec.get("id"),
        "submit_time": datetime.fromtimestamp(rec.get("submitTime", 0), tz=_BJ_TZ).strftime("%m-%d %H:%M") if rec.get("submitTime") else "—",
        "status": rec.get("status", 0),
        "status_label": _status_label(rec.get("status", 0)),
        "time_ms": rec.get("time"),
        "memory_kb": rec.get("memory"),
        "score": rec.get("score"),
        "language": rec.get("language"),
        "code_length": rec.get("sourceCodeLength", len(code)),
        "code_head": head,
        "code_tail": tail,
        "code_full_truncated": (code[:600] + "\n…（后略）") if len(code) > 600 else code,
        "_from_cache": rec.get("_from_cache", False),
        "_error": rec.get("_error"),
    }


def analyze_submission_evolution(
    luogu,
    uid: int,
    records: list[dict],
    *,
    top_n: int = 5,
    max_versions_per_problem: int = 5,
    min_attempts: int = 2,
    concurrency: int = 4,
    verbose: bool = False,
) -> dict:
    """
    v3.9.39 提交代码考古

    Parameters
    ----------
    luogu : pyLuogu.luoguAPI
        已登录的 luogu 客户端（要能调 get_record）
    uid : int
        洛谷 UID
    records : list[dict]
        get_record_list 返回的全部提交记录（每条含 id/submitTime/status/problem.pid/sourceCodeLength）
    top_n : int
        选 TOP N 道"考古价值"最高的题（默认 5）
    max_versions_per_problem : int
        每题最多取最新 N 个版本（默认 5，节省 API）
    min_attempts : int
        至少提交几次才算"考古对象"（默认 2）
    concurrency : int
        get_record 并发数（默认 4，参考现有 _pick_record_for_problem 的 SOURCE_FETCH_CONCURRENCY）

    Returns
    -------
    dict:
      {
        "selected_problems": [
          {
            "pid", "title", "difficulty", "tags",
            "attempts", "final_status", "is_accepted",
            "status_timeline": "v1:WA → v2:WA → v3:AC",  # 给 AI 看
            "code_length_timeline": "500 → 480 → 520",
            "versions": [ {v, status, ...}, ... ],
            "diffs": [ {v_from, v_to, summary, lines_added, lines_removed}, ... ],
            "evolution_score": float,
          }
        ],
        "summary": {
          "total_multi_submit_problems": int,
          "selected_count": int,
          "api_calls": int,
          "cache_hits": int,
        }
      }
    """
    if not records:
        return {"selected_problems": [], "summary": {"total_multi_submit_problems": 0, "selected_count": 0, "api_calls": 0, "cache_hits": 0}}

    # ---- 1. 按 pid 聚合 + 排序 ----
    pid_records: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        pid = (r.get("problem", {}) or {}).get("pid", "")
        if pid:
            pid_records[pid].append(r)
    for pid in pid_records:
        pid_records[pid].sort(key=lambda x: x.get("submitTime", 0))

    # ---- 2. 计算"考古价值"分 + 排序 ----
    candidates: list[tuple[float, str, list[dict]]] = []
    for pid, recs in pid_records.items():
        if len(recs) < min_attempts:
            continue
        score = _evolution_score(recs)
        if score <= 0:
            continue
        candidates.append((score, pid, recs))
    candidates.sort(key=lambda x: -x[0])
    candidates = candidates[:top_n]

    if verbose:
        print(f"[evolution] 候选 {len(candidates)} 道题：")
        for s, pid, recs in candidates:
            print(f"  · {pid} (score={s:.1f}, attempts={len(recs)})")

    # ---- 3. 抓取每题每版的源码（带缓存 + 并发）----
    api_calls = 0
    cache_hits = 0
    selected_problems: list[dict] = []

    def _fetch_problem_versions(pid: str, recs: list[dict]) -> dict:
        nonlocal api_calls, cache_hits
        # 只取最新 N 个版本
        recs_to_fetch = recs[-max_versions_per_problem:]
        versions = []
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futures = {
                ex.submit(_fetch_one_version, luogu, uid, pid, int(r.get("id", 0))): r
                for r in recs_to_fetch
                if r.get("id")
            }
            for fut in as_completed(futures):
                base = futures[fut]
                rec_full = fut.result()
                if rec_full.get("_from_cache"):
                    cache_hits += 1
                elif rec_full.get("sourceCode") and not rec_full.get("_error"):
                    api_calls += 1
                # 把 base 的元数据（time/memory/score 来自 list 接口，可能比 detail 更准确）合并
                merged = {**rec_full}
                for k in ("time", "memory", "score", "language", "sourceCodeLength", "submitTime", "status"):
                    if not merged.get(k) and base.get(k) is not None:
                        merged[k] = base[k]
                versions.append(merged)
        # 按 submitTime 排序
        versions.sort(key=lambda x: x.get("submitTime", 0))
        return {"pid": pid, "versions": versions}

    for score, pid, recs in candidates:
        problem_meta = recs[0].get("problem", {}) or {}
        result = _fetch_problem_versions(pid, recs)
        versions_full = result["versions"]
        if not versions_full:
            continue
        # 构造 version payload（喂 AI 用）
        versions_payload = [_build_version_payload(v, i + 1) for i, v in enumerate(versions_full)]
        # 算 diff（相邻版本两两比对）
        diffs = []
        for i in range(1, len(versions_full)):
            prev = versions_full[i - 1].get("sourceCode") or ""
            curr = versions_full[i].get("sourceCode") or ""
            prev_lines = prev.splitlines() if prev else []
            curr_lines = curr.splitlines() if curr else []
            diffs.append({
                "v_from": i,
                "v_to": i + 1,
                "from_status": _status_label(versions_full[i - 1].get("status", 0)),
                "to_status": _status_label(versions_full[i].get("status", 0)),
                "lines_added": max(0, len(curr_lines) - len(prev_lines)),
                "lines_removed": max(0, len(prev_lines) - len(curr_lines)),
                "byte_delta": len(curr) - len(prev),
                "short_diff": _short_diff(prev, curr),
            })
        # 状态/长度时间线
        status_timeline = " → ".join(f"v{v['v']}:{v['status_label']}" for v in versions_payload)
        length_timeline = " → ".join(str(v["code_length"]) for v in versions_payload)
        final_status = versions_full[-1].get("status", 0)
        selected_problems.append({
            "pid": pid,
            "title": problem_meta.get("title", ""),
            "difficulty": problem_meta.get("difficulty"),
            "tags": problem_meta.get("tags", []),
            "attempts": len(recs),
            "final_status": final_status,
            "is_accepted": (final_status == 12),
            "status_timeline": status_timeline,
            "code_length_timeline": length_timeline,
            "evolution_score": score,
            "versions": versions_payload,
            "diffs": diffs,
        })

    return {
        "selected_problems": selected_problems,
        "summary": {
            "total_multi_submit_problems": sum(1 for recs in pid_records.values() if len(recs) >= min_attempts),
            "selected_count": len(selected_problems),
            "api_calls": api_calls,
            "cache_hits": cache_hits,
        }
    }


# ============================================================================
# v3.9.39 · 把结果渲染成 AI prompt 友好的 Markdown
# ============================================================================
def evolution_to_prompt_block(evolution: dict, max_code_chars_per_version: int = 400) -> str:
    """v3.9.39 · 把 analyze_submission_evolution 的输出组装成 prompt 文本块

    每道题占用 ≈ 600-1200 tokens（5 个版本 × ~150 字 + diff 摘要 + 元数据）
    """
    problems = evolution.get("selected_problems") or []
    if not problems:
        return "（该选手没有多次提交的题目，无代码考古数据）"
    parts = []
    summary = evolution.get("summary", {})
    parts.append(
        f"**统计**：候选多次提交题共 {summary.get('total_multi_submit_problems', 0)} 道，"
        f"本次考古 TOP {summary.get('selected_count', 0)} 道；"
        f"命中缓存 {summary.get('cache_hits', 0)} 次、新调用 {summary.get('api_calls', 0)} 次 luogu API。\n"
    )
    for i, p in enumerate(problems, 1):
        parts.append(f"\n---\n### {i}. {p['pid']} · {p['title']}（提交 {p['attempts']} 次 / 最终 {'✅ AC' if p['is_accepted'] else '❌ 未AC'}）")
        if p.get("difficulty") is not None:
            parts.append(f"- 难度：{p['difficulty']}")
        if p.get("tags"):
            parts.append(f"- 标签：{', '.join(p['tags'][:5])}")
        parts.append(f"- 状态变迁：`{p['status_timeline']}`")
        parts.append(f"- 代码字节数变迁：`{p['code_length_timeline']}`")

        # diff 时间线
        if p["diffs"]:
            parts.append("\n**逐版改进（diff 时间线）**：")
            for d in p["diffs"]:
                arrow = "→"
                tag = "✅" if d["from_status"] in {"WA", "TLE", "RE", "CE", "OLE"} and d["to_status"] == "AC" else ("🔁" if d["from_status"] != d["to_status"] else "🔂")
                parts.append(
                    f"- v{d['v_from']}({d['from_status']}) {arrow} v{d['v_to']}({d['to_status']}) {tag}  "
                    f"行数 {'+' + str(d['lines_added']) if d['lines_added'] else '0'}/"
                    f"{'-' + str(d['lines_removed']) if d['lines_removed'] else '0'}  "
                    f"字节 {'+' + str(d['byte_delta']) if d['byte_delta'] > 0 else str(d['byte_delta'])}"
                )
                if d["short_diff"]:
                    parts.append(f"  - {d['short_diff']}")

        # 关键代码片段
        parts.append("\n**关键代码片段**（每版仅给头尾各一段，避免 prompt 爆炸）：")
        for v in p["versions"]:
            ts = v["submit_time"]
            lang_hint = "C++" if v["language"] == 3 else ("Python" if v["language"] == 1 else f"lang={v['language']}")
            head = v["code_head"][:max_code_chars_per_version]
            tail = v["code_tail"][:max_code_chars_per_version] if v["code_tail"] else ""
            parts.append(
                f"\n> v{v['v']} · {v['status_label']} · {ts} · {lang_hint} · {v['code_length']}B"
            )
            if v.get("_error"):
                parts.append(f"> ⚠ 抓取失败：{v['_error']}（diff 仅基于字节数推算）")
            parts.append(f"```\n{head}\n```")
            if tail and v["code_length"] > max_code_chars_per_version:
                parts.append(f"```\n…\n{tail}\n```")

    return "\n".join(parts)
