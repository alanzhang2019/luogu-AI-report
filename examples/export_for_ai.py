import argparse
import json
import os
import time
from collections import Counter
from typing import Any

import pyLuogu
from pyLuogu.api_helpers import raw_params
from pyLuogu.errors import AuthenticationError, ForbiddenError, RequestError, RateLimitError, ServerError
from pyLuogu.request_helpers import _debug_report


LEVEL_LABELS = {
    "csp_j": "CSP-J",
    "csp_s": "CSP-S",
    "provincial": "省选",
    "noi": "NOI",
}

DETAIL_FETCH_SAMPLE_LIMIT_PASSED = 30
DETAIL_FETCH_SAMPLE_LIMIT_FAILED = 20
DETAIL_FETCH_SLEEP_SECONDS = 1.8
DETAIL_FETCH_MAX_RETRIES = 5
RECORD_LIST_PAGES_TO_TRY = 3
RECORD_LIST_MAX_RETRIES = 4
TRANSIENT_FETCH_MAX_RETRIES = 12
TRANSIENT_FETCH_RETRY_SLEEP_SECONDS = 5.0


def _safe_makedirs_for_file(path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)


def _build_tag_maps(luogu: pyLuogu.luoguAPI) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    res = luogu.get_tags()
    tag_by_id: dict[int, dict[str, Any]] = {}
    for tag in res.tags:
        tag_by_id[tag.id] = {"id": tag.id, "name": tag.name, "type": tag.type, "parent": tag.parent}
    type_by_id: dict[int, dict[str, Any]] = {}
    for type_item in res.types:
        type_by_id[type_item.id] = {"id": type_item.id, "name": type_item.name, "color": type_item.color}
    return tag_by_id, type_by_id


def _empty_level_experience() -> dict[str, dict[str, int | str]]:
    return {
        key: {
            "label": label,
            "solved": 0,
            "by_difficulty": 0,
            "by_origin": 0,
        }
        for key, label in LEVEL_LABELS.items()
    }


def _infer_problem_level_flags(problem: pyLuogu.ProblemSummary, tag_by_id: dict[int, dict[str, Any]]) -> dict[str, tuple[bool, bool]]:
    difficulty_raw = getattr(problem, "difficulty", None)
    difficulty = None if difficulty_raw is None else int(difficulty_raw)
    tag_names: list[str] = []
    origin_tag_names: list[str] = []
    for tag_id in list(getattr(problem, "tags", []) or []):
        tag = tag_by_id.get(int(tag_id)) or {}
        name = str(tag.get("name") or "").strip().lower()
        if not name:
            continue
        tag_names.append(name)
        if int(tag.get("type") or 0) == 3:
            origin_tag_names.append(name)

    all_names = " ".join(tag_names)
    origin_names = " ".join(origin_tag_names)

    difficulty_flags = {
        "csp_j": difficulty is not None and difficulty >= 1,
        "csp_s": difficulty is not None and difficulty >= 4,
        "provincial": difficulty is not None and difficulty >= 6,
        "noi": difficulty is not None and difficulty >= 7,
    }
    origin_flags = {
        "csp_j": any(keyword in all_names for keyword in ("入门", "普及", "noip 普及组", "csp-j")),
        "csp_s": any(keyword in origin_names for keyword in ("提高组", "csp-s", "普及+/提高", "提高+/省选-", "省选", "noi", "hnoi", "noi-")),
        "provincial": any(keyword in origin_names for keyword in ("省选", "省队", "hnoi", "noi-")),
        "noi": any(keyword in origin_names for keyword in ("noi", "ctsc", "ioi", "apio")),
    }
    if origin_flags["noi"]:
        origin_flags["provincial"] = True
        origin_flags["csp_s"] = True
    if origin_flags["provincial"]:
        origin_flags["csp_s"] = True

    flags: dict[str, tuple[bool, bool]] = {}
    for key in LEVEL_LABELS:
        flags[key] = (difficulty_flags[key], origin_flags[key])
    return flags


def _summarize(problems: list[pyLuogu.ProblemSummary], tag_by_id: dict[int, dict[str, Any]]) -> dict[str, Any]:
    difficulty_counter: Counter[int] = Counter()
    tag_counter: Counter[int] = Counter()
    algorithm_tag_counter: Counter[int] = Counter()
    tag_type_counter: Counter[int] = Counter()
    level_experience = _empty_level_experience()

    for p in problems:
        if p.difficulty is not None:
            difficulty_counter[int(p.difficulty)] += 1
        if p.tags:
            for tag_id in p.tags:
                tag_counter[int(tag_id)] += 1
                tag_type = tag_by_id.get(int(tag_id), {}).get("type")
                if tag_type is not None:
                    tag_type_counter[int(tag_type)] += 1
                    if int(tag_type) == 2:
                        algorithm_tag_counter[int(tag_id)] += 1
        for level_key, (by_difficulty, by_origin) in _infer_problem_level_flags(p, tag_by_id).items():
            if by_difficulty or by_origin:
                level_experience[level_key]["solved"] = int(level_experience[level_key]["solved"]) + 1
            if by_difficulty:
                level_experience[level_key]["by_difficulty"] = int(level_experience[level_key]["by_difficulty"]) + 1
            if by_origin:
                level_experience[level_key]["by_origin"] = int(level_experience[level_key]["by_origin"]) + 1

    top_tags = []
    for tag_id, count in tag_counter.most_common(30):
        tag = tag_by_id.get(tag_id)
        top_tags.append(
            {
                "id": tag_id,
                "name": None if tag is None else tag.get("name"),
                "type": None if tag is None else tag.get("type"),
                "count": int(count),
            }
        )

    top_algorithm_tags = []
    for tag_id, count in algorithm_tag_counter.most_common():
        tag = tag_by_id.get(tag_id)
        top_algorithm_tags.append(
            {
                "id": tag_id,
                "name": None if tag is None else tag.get("name"),
                "type": None if tag is None else tag.get("type"),
                "count": int(count),
            }
        )

    return {
        "difficulty_histogram": {str(k): int(v) for k, v in sorted(difficulty_counter.items())},
        "tag_type_histogram": {str(k): int(v) for k, v in sorted(tag_type_counter.items())},
        "top_tags": top_tags,
        "top_algorithm_tags": top_algorithm_tags,
        "level_experience": level_experience,
    }


def _heuristic_suggestions(summary: dict[str, Any]) -> list[str]:
    suggestions: list[str] = []

    difficulty_histogram = summary.get("difficulty_histogram") or {}
    if isinstance(difficulty_histogram, dict):
        easy = int(difficulty_histogram.get("0", 0)) + int(difficulty_histogram.get("1", 0))
        mid = int(difficulty_histogram.get("2", 0)) + int(difficulty_histogram.get("3", 0))
        hard = sum(int(v) for k, v in difficulty_histogram.items() if str(k).isdigit() and int(k) >= 4)
        if easy >= 20 and hard == 0:
            suggestions.append("已完成较多低难度题，建议开始按专题刷中等难度题，并为每个专题总结一份模板与常见坑点。")
        if mid >= 20 and hard <= 5:
            suggestions.append("中等题量已上来，建议逐步加入少量高难题作为周挑战，重点复盘思路而不是堆题量。")

    top_tags = summary.get("top_tags") or []
    if isinstance(top_tags, list) and len(top_tags) >= 10:
        suggestions.append("已刷题标签分布偏向少数方向时，建议补齐薄弱专题：每个新标签先做 3-5 道典型题并输出错因与方法总结。")

    if not suggestions:
        suggestions.append("建议用“题目-代码-错误点-可推广模板”四段式复盘最近 10 次提交，生成可复用的解题清单。")

    return suggestions


def _pick_record_for_problem(
        luogu: pyLuogu.luoguAPI,
        uid: int,
        pid: str,
        max_records_to_try: int,
        *,
        require_source_code: bool = True,
        detail_fetch_state: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    def _to_summary_record(record_obj: Any) -> dict[str, Any]:
        summary_json = record_obj.to_json() if hasattr(record_obj, "to_json") else dict(record_obj)
        summary_json.setdefault("sourceCode", None)
        return summary_json

    def _merge_record_dict(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
        merged = dict(base)
        for key, value in update.items():
            if value is not None:
                merged[key] = value
        merged.setdefault("sourceCode", None)
        return merged

    def _is_blocking_detail_error(exc: Exception) -> bool:
        if isinstance(exc, (AuthenticationError, ForbiddenError)):
            return True
        if isinstance(exc, RateLimitError):
            return False
        if isinstance(exc, RequestError) and getattr(exc, "status_code", None) == 429:
            return False
        message = str(exc).strip().lower()
        return ("need login" in message) or ("auth" in message and "login" in message)

    def _is_transient_detail_error(exc: Exception) -> bool:
        if isinstance(exc, (RateLimitError, ServerError)):
            return True
        if isinstance(exc, RequestError):
            status_code = getattr(exc, "status_code", None)
            if status_code in {408, 425, 429, 500, 502, 503, 504}:
                return True
            if status_code is None:
                message = str(exc).strip().lower()
                if ("need login" in message) or ("auth" in message and "login" in message):
                    return False
                transient_keywords = (
                    "request error",
                    "failed to send request",
                    "timeout",
                    "timed out",
                    "connection",
                    "network",
                    "server error",
                    "temporarily unavailable",
                )
                return any(keyword in message for keyword in transient_keywords)
        return False

    def _build_list_level_fallback(reason: str) -> dict[str, Any]:
        return {
            "sourceCode": None,
            "_detail_requested": bool(require_source_code),
            "_detail_skipped": reason,
            "_record_list_unavailable": True,
        }

    state = detail_fetch_state if isinstance(detail_fetch_state, dict) else {}
    if state.get("stop_detail_fetch"):
        return _build_list_level_fallback(str(state.get("last_detail_error") or "detail fetch stopped"))
    record_list = None
    merged_records: list[Any] = []
    seen_record_ids: set[str] = set()
    last_list_exc: Exception | None = None
    for page in range(1, RECORD_LIST_PAGES_TO_TRY + 1):
        last_list_exc = None
        max_attempts = max(RECORD_LIST_MAX_RETRIES, TRANSIENT_FETCH_MAX_RETRIES)
        for attempt in range(max_attempts):
            if DETAIL_FETCH_SLEEP_SECONDS > 0:
                base_sleep = DETAIL_FETCH_SLEEP_SECONDS * (1 + min(attempt, RECORD_LIST_MAX_RETRIES - 1) * 0.7)
                if attempt >= RECORD_LIST_MAX_RETRIES:
                    base_sleep = max(base_sleep, TRANSIENT_FETCH_RETRY_SLEEP_SECONDS)
                time.sleep(base_sleep)
            try:
                record_list = luogu.get_record_list(page=page, uid=uid, pid=pid, user=str(uid))
                last_list_exc = None
                break
            except Exception as exc:
                last_list_exc = exc
                if _is_transient_detail_error(exc):
                    continue
                break
        if record_list and getattr(record_list, "records", None):
            for r in record_list.records:
                rid = str(getattr(r, "id", ""))
                if rid and rid not in seen_record_ids:
                    merged_records.append(r)
                    seen_record_ids.add(rid)
            if merged_records:
                break

    if not merged_records and last_list_exc is not None:
        _debug_report(
            "D",
            "examples/export_for_ai.py:_pick_record_for_problem:list",
            "[DEBUG] record list fetch failed before fallback",
            {
                "uid": uid,
                "pid": pid,
                "require_source_code": bool(require_source_code),
                "error": str(last_list_exc),
                "state_stop_detail_fetch": bool(state.get("stop_detail_fetch")),
            },
        )
        if _is_blocking_detail_error(last_list_exc):
            state["stop_detail_fetch"] = True
            state["last_detail_error"] = str(last_list_exc)
            return _build_list_level_fallback(str(last_list_exc))
        raise last_list_exc

    if not merged_records:
        _debug_report(
            "D",
            "examples/export_for_ai.py:_pick_record_for_problem:empty",
            "[DEBUG] record list returned no records",
            {
                "uid": uid,
                "pid": pid,
                "require_source_code": bool(require_source_code),
            },
        )
        return None

    tried = 0
    best_effort_record: dict[str, Any] | None = None
    if state.get("stop_detail_fetch"):
        first_record = merged_records[0]
        best_effort_record = _to_summary_record(first_record)
        best_effort_record["_detail_requested"] = bool(require_source_code)
        best_effort_record["_detail_skipped"] = str(state.get("last_detail_error") or "detail fetch stopped")
        _debug_report(
            "D",
            "examples/export_for_ai.py:_pick_record_for_problem:skip",
            "[DEBUG] detail fetch skipped and summary fallback kept",
            {
                "uid": uid,
                "pid": pid,
                "error": str(state.get("last_detail_error") or ""),
            },
        )
        return best_effort_record

    for record in merged_records:
        if tried >= max_records_to_try:
            break
        tried += 1
        summary_json = _to_summary_record(record)
        summary_json["_detail_requested"] = bool(require_source_code)
        if best_effort_record is None and isinstance(summary_json, dict):
            best_effort_record = dict(summary_json)
        if not require_source_code:
            continue

        last_exc: Exception | None = None
        max_attempts = max(DETAIL_FETCH_MAX_RETRIES, TRANSIENT_FETCH_MAX_RETRIES)
        for attempt in range(max_attempts):
            if DETAIL_FETCH_SLEEP_SECONDS > 0:
                base_sleep = DETAIL_FETCH_SLEEP_SECONDS * (1 + min(attempt, DETAIL_FETCH_MAX_RETRIES - 1) * 0.5)
                if attempt >= DETAIL_FETCH_MAX_RETRIES:
                    base_sleep = max(base_sleep, TRANSIENT_FETCH_RETRY_SLEEP_SECONDS)
                time.sleep(base_sleep)
            try:
                detail = luogu.get_record(str(record.id)).record
                detail_json = detail.to_json()
                code = detail.sourceCode
                detail_json["sourceCode"] = code
                detail_json["_detail_requested"] = True
                if code:
                    return detail_json
                best_effort_record = _merge_record_dict(best_effort_record or summary_json, detail_json)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                if best_effort_record is not None:
                    best_effort_record.setdefault("sourceCode", None)
                    best_effort_record["_detail_error"] = str(exc)
                if _is_transient_detail_error(exc):
                    continue
                if _is_blocking_detail_error(exc):
                    state["stop_detail_fetch"] = True
                    state["last_detail_error"] = str(exc)
                    _debug_report(
                        "D",
                        "examples/export_for_ai.py:_pick_record_for_problem:block",
                        "[DEBUG] detail fetch triggered circuit breaker",
                        {
                            "uid": uid,
                            "pid": pid,
                            "record_id": str(getattr(record, "id", "")),
                            "error": str(exc),
                        },
                    )
                    break
        if state.get("stop_detail_fetch"):
            break
    return best_effort_record


def _is_code_file(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in {
        ".cpp",
        ".cc",
        ".c",
        ".py",
        ".java",
        ".js",
        ".ts",
        ".rs",
        ".go",
        ".cs",
        ".kt",
        ".pas",
        ".php",
        ".rb",
        ".swift",
    }


def _index_local_code(code_dir: str) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for root, _, files in os.walk(code_dir):
        for name in files:
            full = os.path.join(root, name)
            if not _is_code_file(full):
                continue
            pid = os.path.splitext(name)[0].upper()
            if not pid:
                continue
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            prev = index.get(pid)
            if prev is None or mtime > float(prev.get("mtime", 0)):
                index[pid] = {"path": full, "mtime": mtime}
    return index


def _read_text_limited(path: str, max_chars: int) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = f.read(max_chars + 1)
    except OSError:
        return None
    if len(data) > max_chars:
        return data[:max_chars]
    return data


def _load_cookies(cookies_path: str | None) -> pyLuogu.LuoguCookies:
    raw_env = os.environ.get("LUOGU_COOKIES_JSON")
    if raw_env:
        try:
            data = json.loads(raw_env)
        except json.JSONDecodeError:
            raise ValueError("LUOGU_COOKIES_JSON is not valid JSON") from None
        return pyLuogu.LuoguCookies(data)

    client_id = os.environ.get("LUOGU_CLIENT_ID")
    uid = os.environ.get("LUOGU_UID")
    if client_id and uid:
        return pyLuogu.LuoguCookies({"__client_id": client_id, "_uid": uid})

    if not cookies_path:
        raise ValueError("cookies are required: set LUOGU_COOKIES_JSON or LUOGU_CLIENT_ID+LUOGU_UID or pass --cookies")
    return pyLuogu.LuoguCookies.from_file(cookies_path)


def _export(args: argparse.Namespace) -> None:
    pyLuogu.set_log_level(args.log_level)
    cookies = _load_cookies(args.cookies)
    luogu = pyLuogu.luoguAPI(cookies=cookies)

    me = luogu.me()
    uid = int(me.uid)

    tag_by_id, type_by_id = _build_tag_maps(luogu)
    local_code_index: dict[str, dict[str, Any]] = {}
    if args.code_dir:
        local_code_index = _index_local_code(args.code_dir)

    practice = luogu.get_user_practice(uid)
    solved: list[pyLuogu.ProblemSummary] = []
    raw = practice.data if isinstance(practice.data, dict) else None
    passed = raw.get("passed") if isinstance(raw, dict) else None
    if isinstance(passed, list):
        for item in passed:
            if not isinstance(item, dict):
                continue
            pid = item.get("pid")
            if not pid:
                continue
            solved.append(
                pyLuogu.ProblemSummary(
                    {
                        "pid": str(pid),
                        "title": item.get("title") or item.get("name") or "",
                        "difficulty": item.get("difficulty"),
                        "type": item.get("type"),
                        "submitted": True,
                        "accepted": True,
                        "tags": [],
                        "totalSubmit": None,
                        "totalAccepted": None,
                        "flag": None,
                        "fullScore": None,
                    }
                )
            )
    else:
        solved = list(practice.problems)
    solved.sort(key=lambda p: (p.difficulty if p.difficulty is not None else 10, p.pid))
    if args.max_problems is not None:
        solved = solved[: max(0, int(args.max_problems))]

    webauthn_required = False
    try:
        probe = luogu._send_request(endpoint="record/list", params=raw_params(page=1, user=str(uid)))
        if isinstance(probe, dict) and probe.get("webauthn") is not None:
            webauthn_required = True
    except Exception:
        webauthn_required = False

    export_items: list[dict[str, Any]] = []
    for index, problem in enumerate(solved, start=1):
        if index % 10 == 0 or index == 1 or index == len(solved):
            print(f"[{index}/{len(solved)}] {problem.pid} {problem.title}")

        local_code = None
        local_meta = local_code_index.get(problem.pid.upper())
        if local_meta is not None:
            content = _read_text_limited(str(local_meta["path"]), int(args.max_code_chars))
            local_code = {
                "source": "local",
                "path": local_meta.get("path"),
                "mtime": local_meta.get("mtime"),
                "content": content,
            }

        record = None
        if webauthn_required:
            record = {"error": "webauthn_required", "pid": problem.pid}
        else:
            try:
                record = _pick_record_for_problem(
                    luogu=luogu,
                    uid=uid,
                    pid=problem.pid,
                    max_records_to_try=int(args.records_per_problem),
                )
            except Exception as e:
                record = {"error": str(e), "pid": problem.pid}
                time.sleep(0.2)

        export_items.append(
            {
                "problem": problem.to_json(),
                "record": record,
                "local_code": local_code,
            }
        )

    summary = _summarize(solved, tag_by_id=tag_by_id)
    suggestions = _heuristic_suggestions(summary)

    payload = {
        "schema_version": 1,
        "generated_at": int(time.time()),
        "user": me.to_json(),
        "solved_count": len(solved),
        "webauthn_required_for_record_list": webauthn_required,
        "tags": {"by_id": tag_by_id, "types_by_id": type_by_id},
        "summary": summary,
        "heuristic_suggestions": suggestions,
        "items": export_items,
    }

    _safe_makedirs_for_file(args.out)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"Exported: {os.path.abspath(args.out)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cookies", default="cookies.json")
    parser.add_argument("--out", default="luogu_export.json")
    parser.add_argument("--max-problems", type=int, default=30)
    parser.add_argument("--records-per-problem", type=int, default=3)
    parser.add_argument("--code-dir", default=None)
    parser.add_argument("--max-code-chars", type=int, default=20000)
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args()
    _export(args)


if __name__ == "__main__":
    main()
