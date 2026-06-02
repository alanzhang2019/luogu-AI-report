import argparse
import json
import os
import time
from collections import Counter
from typing import Any

import pyLuogu
from pyLuogu.api_helpers import raw_params


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


def _summarize(problems: list[pyLuogu.ProblemSummary], tag_by_id: dict[int, dict[str, Any]]) -> dict[str, Any]:
    difficulty_counter: Counter[int] = Counter()
    tag_counter: Counter[int] = Counter()
    tag_type_counter: Counter[int] = Counter()

    for p in problems:
        if p.difficulty is not None:
            difficulty_counter[int(p.difficulty)] += 1
        if p.tags:
            for tag_id in p.tags:
                tag_counter[int(tag_id)] += 1
                tag_type = tag_by_id.get(int(tag_id), {}).get("type")
                if tag_type is not None:
                    tag_type_counter[int(tag_type)] += 1

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

    return {
        "difficulty_histogram": {str(k): int(v) for k, v in sorted(difficulty_counter.items())},
        "tag_type_histogram": {str(k): int(v) for k, v in sorted(tag_type_counter.items())},
        "top_tags": top_tags,
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
) -> dict[str, Any] | None:
    record_list = luogu.get_record_list(page=1, uid=uid, pid=pid, user=str(uid))
    if not record_list.records:
        return None

    tried = 0
    for record in record_list.records:
        if tried >= max_records_to_try:
            break
        tried += 1
        detail = luogu.get_record(str(record.id)).record
        detail_json = detail.to_json()
        code = detail.sourceCode
        detail_json["sourceCode"] = code
        if code:
            return detail_json
        if tried == 1:
            return detail_json
    return None


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
