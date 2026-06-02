from typing import Any


PROBLEM_SETTINGS_KEYS = {
    "title",
    "background",
    "description",
    "inputFormat",
    "outputFormat",
    "samples",
    "hint",
    "translation",
    "needsTranslation",
    "acceptSolution",
    "allowDataDownload",
    "difficulty",
    "showScore",
    "flag",
    "tags",
}


def luogu_problem_settings(settings: Any) -> dict[str, Any]:
    if hasattr(settings, "to_json"):
        settings = settings.to_json()
    return {key: settings[key] for key in PROBLEM_SETTINGS_KEYS if key in settings}


def create_problem_payload(settings: Any, type_: str, provider_id: int | None) -> dict[str, Any]:
    return {
        "settings": luogu_problem_settings(settings),
        "type": type_,
        "providerID": provider_id,
    }


def update_problem_payload(settings: Any) -> dict[str, Any]:
    return {"settings": luogu_problem_settings(settings)}


def flatten_paged_field(data: dict[str, Any], source: str, target: str | None = None) -> dict[str, Any]:
    target = target or source
    page = data[source]
    data["count"] = page["count"]
    data["perPage"] = page["perPage"]
    data[target] = page["result"]
    return data


def raw_response(data: Any) -> dict[str, Any]:
    return {"data": data}


def empty_response(data: dict[str, Any] | None = None) -> dict[str, Any]:
    if data is None:
        return {"ok": True, "_empty": True}
    if "_empty" in data:
        data["ok"] = bool(data["_empty"])
        return data
    data["ok"] = True
    data["_empty"] = False
    return data


def normalize_optional_paged_field(data: dict[str, Any], candidates: tuple[str, ...], target: str) -> dict[str, Any]:
    for source in candidates:
        page = data.get(source)
        if isinstance(page, dict) and "result" in page:
            return flatten_paged_field(data, source, target)
        if isinstance(page, list):
            data[target] = page
            data.setdefault("count", len(page))
            data.setdefault("perPage", len(page))
            return data
    if target not in data:
        data[target] = []
    data.setdefault("count", len(data[target]))
    data.setdefault("perPage", len(data[target]))
    return data


def normalize_problem_list(data: dict[str, Any]) -> dict[str, Any]:
    data = flatten_paged_field(data, "problems")
    problems = data.get("problems")
    if isinstance(problems, list):
        for item in problems:
            if isinstance(item, dict) and item.get("title") is None and item.get("name") is not None:
                item["title"] = item.get("name")
    return data


def normalize_problem_data(data: dict[str, Any]) -> dict[str, Any]:
    limits = data["problem"].get("limits")
    if isinstance(limits, dict):
        data["problem"]["limits"] = list(zip(limits["time"], limits["memory"]))
    if data.get("problem") is not None:
        problem = data["problem"]
        if isinstance(problem, dict) and problem.get("title") is None and problem.get("name") is not None:
            problem["title"] = problem.get("name")
    return data


def normalize_problem_settings_legacy(data: dict[str, Any]) -> dict[str, Any]:
    data["problemDetails"] = data["problem"]
    data["problemSettings"] = data["setting"]
    data["problemSettings"]["comment"] = data["problem"]["comment"]
    provider = data["problem"]["provider"]
    data["problemSettings"]["providerID"] = provider.get("uid") or provider.get("id")
    data["testCaseSettings"] = {
        "cases": data["testCases"],
        "scoringStrategy": data["scoringStrategy"],
        "subtaskScoringStrategies": data["subtaskScoringStrategies"],
        "showSubtask": data["showSubtask"],
    }
    return data


def normalize_problem_settings(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("problemSettings") is not None:
        return data

    if data.get("setting") is not None:
        return normalize_problem_settings_legacy(data)

    if data.get("settings") is not None:
        data["problemSettings"] = data["settings"]
        if data.get("problem") is not None:
            data["problemDetails"] = data["problem"]
        return data

    return data


def normalize_problem_solutions(data: dict[str, Any]) -> dict[str, Any]:
    return flatten_paged_field(data, "solutions")


def normalize_user_data(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("teams") is not None:
        data["teams"] = [x.get("team") for x in data["teams"]]
    if data.get("eloMax") is not None and data.get("user") is not None:
        data["user"]["eloMax"] = data["eloMax"]
    return data


def normalize_problem_set(data: dict[str, Any]) -> dict[str, Any]:
    data["training"]["problems"] = [x.get("problem") for x in data["training"]["problems"]]
    return data


def normalize_problem_set_list(data: dict[str, Any]) -> dict[str, Any]:
    return flatten_paged_field(data, "trainings")


def normalize_contest(data: dict[str, Any]) -> dict[str, Any]:
    data["contest"]["problems"] = [x.get("problem") for x in data["contestProblems"]]
    data["contest"]["isScoreboardFrozen"] = data["isScoreboardFrozen"]
    return data


def normalize_contest_list(data: dict[str, Any]) -> dict[str, Any]:
    return flatten_paged_field(data, "contests")


def normalize_discussion(data: dict[str, Any]) -> dict[str, Any]:
    data["perPage"] = data["replies"]["perPage"]
    data["count"] = data["replies"]["count"]
    data["replies"] = data["replies"]["result"]
    return data


def normalize_activity(data: dict[str, Any]) -> dict[str, Any]:
    data["activities"] = data["feeds"]["result"]
    data["perPage"] = data["feeds"]["perPage"]
    data["count"] = data["feeds"]["count"]
    return data


def normalize_team_members(data: dict[str, Any]) -> dict[str, Any]:
    return flatten_paged_field(data, "members")


def normalize_articles(data: dict[str, Any]) -> dict[str, Any]:
    return normalize_optional_paged_field(data, ("articles", "items"), "articles")


def normalize_article_replies(data: dict[str, Any]) -> dict[str, Any]:
    return normalize_optional_paged_field(data, ("replies", "comments"), "replies")


def normalize_blogs(data: dict[str, Any]) -> dict[str, Any]:
    return normalize_optional_paged_field(data, ("blogs", "articles", "items"), "blogs")


def normalize_blog(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("blog") is None and data.get("article") is not None:
        data["blog"] = data["article"]
    return data


def normalize_blog_replies(data: dict[str, Any]) -> dict[str, Any]:
    return normalize_optional_paged_field(data, ("replies", "comments"), "replies")


def normalize_chat_records(data: dict[str, Any]) -> dict[str, Any]:
    return normalize_optional_paged_field(data, ("records", "messages", "chats"), "records")


def normalize_images(data: dict[str, Any]) -> dict[str, Any]:
    return normalize_optional_paged_field(data, ("images",), "images")


def normalize_rankings(data: dict[str, Any]) -> dict[str, Any]:
    return normalize_optional_paged_field(data, ("users", "ranking"), "users")


def normalize_notifications(data: dict[str, Any]) -> dict[str, Any]:
    return normalize_optional_paged_field(data, ("notifications",), "notifications")


def normalize_pastes(data: dict[str, Any]) -> dict[str, Any]:
    return normalize_optional_paged_field(data, ("pastes",), "pastes")


def normalize_records(data: dict[str, Any]) -> dict[str, Any]:
    return normalize_optional_paged_field(data, ("records",), "records")


def normalize_teams(data: dict[str, Any]) -> dict[str, Any]:
    return normalize_optional_paged_field(data, ("teams",), "teams")


def normalize_themes(data: dict[str, Any]) -> dict[str, Any]:
    return normalize_optional_paged_field(data, ("themes",), "themes")


def normalize_user_practice(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("problems") is not None:
        return normalize_optional_paged_field(data, ("problems",), "problems")

    passed = data.get("passed")
    submitted = data.get("submitted")
    if isinstance(passed, list) or isinstance(submitted, list):
        passed_list = passed if isinstance(passed, list) else []
        submitted_list = submitted if isinstance(submitted, list) else []

        problems: list[dict[str, Any]] = []
        passed_ids: set[str] = set()

        for item in passed_list:
            if not isinstance(item, dict):
                continue
            pid = item.get("pid")
            if not pid:
                continue
            pid = str(pid)
            passed_ids.add(pid)
            problems.append(
                {
                    "pid": pid,
                    "title": item.get("title") or item.get("name") or "",
                    "difficulty": item.get("difficulty"),
                    "type": item.get("type"),
                    "submitted": True,
                    "accepted": True,
                }
            )

        for item in submitted_list:
            if not isinstance(item, dict):
                continue
            pid = item.get("pid")
            if not pid:
                continue
            pid = str(pid)
            if pid in passed_ids:
                continue
            problems.append(
                {
                    "pid": pid,
                    "title": item.get("title") or item.get("name") or "",
                    "difficulty": item.get("difficulty"),
                    "type": item.get("type"),
                    "submitted": True,
                    "accepted": False,
                }
            )

        normalized = raw_response(data)
        normalized["problems"] = problems
        normalized["count"] = len(problems)
        normalized["perPage"] = len(problems)
        return normalized

    normalized = raw_response(data)
    normalized["problems"] = []
    normalized["count"] = 0
    normalized["perPage"] = 0
    return normalized


def normalize_downloadable_testcases(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("testcases") is None:
        for key in ("testCases", "cases", "data"):
            if isinstance(data.get(key), list):
                data["testcases"] = data[key]
                break
    data.setdefault("testcases", [])
    data["data"] = data
    return data
