import asyncio
from typing import Any, List, Literal, Callable, TypeVar, cast

import httpx

from .types import *
from .errors import *
from .api_helpers import *
from .normalizers import *
from .transport import AsyncLuoguTransportMixin
from . import logger

_TResponse = TypeVar("_TResponse", bound=Response)


def _json_payload(payload: JsonMapping | None) -> JsonObject | None:
    if payload is None:
        return None
    return cast(JsonObject, dict(payload))


class asyncLuoguAPI(AsyncLuoguTransportMixin):
    def __init__(
            self,
            base_url="https://www.luogu.com.cn",
            cookies: LuoguCookies | None = None,
            timeout: float | httpx.Timeout | None = 10,
            max_retries: int = 5,
    ):
        self._init_transport(base_url, cookies, max_retries)
        self.client = httpx.AsyncClient(
            timeout=timeout,
            cookies=self.cookies,
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.aclose()
        return False

    def _post_captcha(self, captcha: str):
        raise NotImplementedError
    
    async def login(
            self, user_name: str, password: str,
            captcha: Literal["input", "ocr"],
            two_step_verify: Literal["google", "email"] | None = None
    ) -> bool:
        raise NotImplementedError

    async def logout(self):
        res = await self._send_request(endpoint=api_route("auth_logout"), method="POST")
        self.x_csrf_token = None
        return EmptyResponse(empty_response(res))

    async def get_problem_list(
            self,
            page: int | None = None,
            orderBy: str | None = None,
            order: Literal["asc", "desc"] | None = None,
            keyword: str | None = None,
            content: bool | None = None,
            _type: ProblemType | None = None,
            difficulty: int | None = None,
            tag: str | None = None,
            params: ProblemListRequestParams | None = None
    ) -> ProblemListRequestResponse:
        if params is None:
            params = problem_list_params(page, orderBy, order, keyword, content, _type, difficulty, tag)
        res = await self._send_request(endpoint=PROBLEM_LIST_ENDPOINT, params=params)

        return ProblemListRequestResponse(normalize_problem_list(res))

    async def get_team_problem_list(
            self, tid: int,
            page: int | None = None,
            keyword: str | None = None,
            orderBy: Literal["pid", "name"] | None = None,
            order: Literal["asc", "desc"] | None = None,
    ) -> ProblemListRequestResponse:
        params = team_problem_list_params(page, keyword, orderBy, order)
        res = await self._send_request(
            endpoint=team_problems_endpoint(tid), 
            params=params
        )

        return ProblemListRequestResponse(normalize_problem_list(res))

    async def get_problem(
            self, pid: str,
            contest_id: int | None = None
    ) -> ProblemDataRequestResponse:
        params = problem_request_params(contest_id)
        res = await self._send_request(endpoint=problem_endpoint(pid), params=params)

        return ProblemDataRequestResponse(normalize_problem_data(res))

    async def get_problem_settings_legacy(
            self, pid: str,
    ) -> ProblemSettingsRequestResponse:
        res = await self._send_request(endpoint=problem_settings_legacy_endpoint(pid))
        
        return ProblemSettingsRequestResponse(normalize_problem_settings_legacy(res))

    async def get_problem_settings(self, pid: str) -> ProblemSettingsRequestResponse:
        res = await self._send_request(endpoint=problem_settings_endpoint(pid))

        return ProblemSettingsRequestResponse(normalize_problem_settings(res))

    async def update_problem_settings(
            self, pid: str,
            new_settings: ProblemSettings,
    ) -> ProblemModifiedResponse:
        res = await self._send_request(
            endpoint=problem_edit_endpoint(pid),
            method="POST",
            data=update_problem_payload(new_settings)
        )

        return ProblemModifiedResponse(res)

    async def update_testcases_settings(
            self, pid: str,
            new_settings: TestCaseSettings
    ) -> UpdateTestCasesSettingsResponse:
        res = await self._send_request(
            endpoint=problem_edit_testcase_endpoint(pid),
            method="POST",
            data=new_settings.to_json()
        )

        return UpdateTestCasesSettingsResponse(res)

    async def create_problem(
            self, settings: ProblemSettings,
            tid : int | None = None,

    ) -> ProblemModifiedResponse:
        _type = "U" if tid is None else "T"
        res = await self._send_request(
            endpoint=PROBLEM_NEW_ENDPOINT,
            method="POST",
            data=create_problem_payload(settings, _type, tid)
        )

        return ProblemModifiedResponse(res)

    async def delete_problem(
            self, pid: str,
    ) -> bool:
        res = await self._send_request(
            endpoint=problem_delete_endpoint(pid),
            method="POST",
            data={}
        )

        return res["_empty"]

    async def transfer_problem(
            self, pid: str,
            target: TransferProblemType = "U",
            is_clone: bool = False
    ) -> ProblemModifiedResponse:
        res = await self._send_request(
            endpoint=problem_transfer_endpoint(pid),
            method="POST",
            data=transfer_problem_payload(target, is_clone)
        )

        return ProblemModifiedResponse(res)

    async def download_testcases(
            self, pid: int
    ):
        raise NotImplementedError
    
    async def upload_testcases(
            self, pid: int,
            path: str
    ):
        raise NotImplementedError

    async def get_problem_solutions(self, pid: str, page: int | None = None) -> ProblemSolutionRequestResponse:
        params = list_params(page)
        res = await self._send_request(endpoint=problem_solution_endpoint(pid), params=params)

        return ProblemSolutionRequestResponse(normalize_problem_solutions(res))

    async def get_problem_set(self, id: int) -> ProblemSetDataRequestResponse:
        res = await self._send_request(endpoint=problem_set_endpoint(id))
        return ProblemSetDataRequestResponse(normalize_problem_set(res))
    
    async def get_problem_set_list(
            self,
            page: int | None = None,
            keyword: str | None = None,
            type: ProblemSetType | None = None, 
            params: ProblemSetListRequestParams | None = None
    ):
        if params is None:
            params = problem_set_list_params(page, keyword, type)
        res = await self._send_request(endpoint=PROBLEM_SET_LIST_ENDPOINT, params=params)
        return ProblemSetListRequestResponse(normalize_problem_set_list(res))
    
    async def get_user(self, uid: int) -> UserDataRequestResponse:
        res = await self._send_request(endpoint=user_endpoint(uid))
        return UserDataRequestResponse(normalize_user_data(res))

    async def get_user_info(self, uid: int) -> UserDetails:
        res = await self._send_request(endpoint=user_info_endpoint(uid))
        return UserDetails(res["user"])
    
    async def get_user_following_list(self, uid: int, page: int | None = None) -> List[UserDetails]:
        params = user_list_params(uid, page)
        res = await self._send_request(endpoint=user_followings_endpoint(), params=params)
        return [UserDetails(user) for user in res["users"]["result"]]

    async def get_user_follower_list(self, uid: int, page: int | None = None) -> List[UserDetails]:
        params = user_list_params(uid, page)
        res = await self._send_request(endpoint=user_followers_endpoint(), params=params)
        return [UserDetails(user) for user in res["users"]["result"]]

    async def get_user_blacklist(self, uid: int, page: int | None = None) -> List[UserDetails]:
        params = user_list_params(uid, page)
        res = await self._send_request(endpoint=user_blacklist_endpoint(), params=params)
        return [UserDetails(user) for user in res["users"]["result"]]
    
    async def search_user(self, keyword: str) -> List[UserSummary]:
        params = UserSearchRequestParams({"keyword" : keyword})
        res = await self._send_request(endpoint=USER_SEARCH_ENDPOINT, params=params)
        return [UserSummary(user) for user in res["users"]]

    async def get_contest(self, id: int) -> ContestDataRequestResponse:
        res = await self._send_request(endpoint=contest_endpoint(id))

        return ContestDataRequestResponse(normalize_contest(res))

    async def get_contest_list(
            self,
            page: int | None = None,
            name: str | None = None,
            method: int | None = None,
            public: int | None = None,
    ) -> ContestListRequestResponse:
        params = contest_list_params(page, name, method, public)
        res = await self._send_request(endpoint=CONTEST_LIST_ENDPOINT, params=params)
        return ContestListRequestResponse(normalize_contest_list(res))

    async def get_disscussion(self,
            id: int,
            page: int | None = None,
            orderBy: int | None = None,
    ) -> DiscussionRequestResponse:
        params = discussion_params(page, orderBy)
        res = await self._send_request(endpoint=discussion_endpoint(id), params=params)

        return DiscussionRequestResponse(normalize_discussion(res))

    async def get_activity(
            self,
            uid: int,
            page: int | None = None
    ) -> ActivityRequestResponse:
        params = activity_params(uid, page)
        res = await self._send_request(endpoint=ACTIVITY_ENDPOINT, params=params)

        return ActivityRequestResponse(normalize_activity(res))

    async def get_team(self, tid: int) -> TeamDataRequestResponse:
        res = await self._send_request(endpoint=team_endpoint(tid))
        return TeamDataRequestResponse(res)

    async def get_team_member_list(self, tid: int) -> TeamMemberRequestResponse:
        res = await self._send_request(endpoint=team_members_endpoint(tid))
        return TeamMemberRequestResponse(normalize_team_members(res))

    async def me(self) -> UserDetails:
        if self.cookies is None or "_uid" not in self.cookies:
            raise AuthenticationError("Need Login")
        return (await self.get_user(int(self.cookies["_uid"].split("_")[0]))).user

    async def get_created_problem_list(
            self, page: int | None = None
    ) -> ProblemListRequestResponse:
        params = list_params(page)
        res = await self._send_request(endpoint=CREATED_PROBLEMS_ENDPOINT, params=params)

        return ProblemListRequestResponse(normalize_problem_list(res))
    
    async def get_created_problem_set_list(self, page: int | None = None) -> ProblemSetListRequestResponse:
        params = list_params(page)
        res = await self._send_request(endpoint=CREATED_PROBLEM_SETS_ENDPOINT, params=params)

        return ProblemSetListRequestResponse(normalize_problem_set_list(res))
    
    async def get_created_contest_list(self, page: int | None = None) -> ContestListRequestResponse:
        params = list_params(page)
        res = await self._send_request(endpoint=CREATED_CONTESTS_ENDPOINT, params=params)
        return ContestListRequestResponse(normalize_contest_list(res))

    async def get_team_problem_set_list(self, tid: int, page: int | None = None) -> ProblemSetListRequestResponse:
        params = list_params(page)
        res = await self._send_request(endpoint=team_problem_sets_endpoint(tid), params=params)
        return ProblemSetListRequestResponse(normalize_problem_set_list(res))

    async def get_team_contest_list(self, tid: int, page: int | None = None) -> ContestListRequestResponse:
        params = list_params(page)
        res = await self._send_request(endpoint=team_contests_endpoint(tid), params=params)
        return ContestListRequestResponse(normalize_contest_list(res))

    async def get_created_contest(self, id: int) -> RawDataResponse:
        return await self._request_route("contest_created", path_params={"id": id})

    async def get_team_member_page(self, tid: int, page: int | None = None) -> TeamMemberRequestResponse:
        return await self._typed_route(
            "team_member_page",
            TeamMemberRequestResponse,
            path_params={"id": tid},
            params=raw_params(page=page),
            normalizer=normalize_team_members,
        )

    async def get_team_problem_page(
            self,
            tid: int,
            page: int | None = None,
            keyword: str | None = None,
            orderBy: Literal["pid", "name"] | None = None,
            order: Literal["asc", "desc"] | None = None,
    ) -> ProblemListRequestResponse:
        return await self._typed_route(
            "team_problem_page",
            ProblemListRequestResponse,
            path_params={"id": tid},
            params=team_problem_list_params(page, keyword, orderBy, order),
            normalizer=normalize_problem_list,
        )

    async def get_team_training_page(self, tid: int, page: int | None = None) -> ProblemSetListRequestResponse:
        return await self._typed_route(
            "team_training_page",
            ProblemSetListRequestResponse,
            path_params={"id": tid},
            params=list_params(page),
            normalizer=normalize_problem_set_list,
        )

    async def get_team_contest_page(self, tid: int, page: int | None = None) -> ContestListRequestResponse:
        return await self._typed_route(
            "team_contest_page",
            ContestListRequestResponse,
            path_params={"id": tid},
            params=list_params(page),
            normalizer=normalize_contest_list,
        )

    async def submit_code(
            self,
            pid: str,
            code: str,
            contest_id: int | None = None,
            lang: int | None = None,
            enableO2: bool | int = True,
            capture_handler: Callable[[bytes, int], str] | None = None
    ) -> SubmitCodeResponse:
        captcha_text = ""
        for attempt in range(self.max_retries):
            try:
                await self._get_csrf(f"/problem/{pid}")
                res = await self._send_request(
                    endpoint=problem_submit_endpoint(pid),
                    params=problem_request_params(contest_id),
                    method="POST",
                    data=submit_code_payload(code, lang, enableO2, captcha_text)
                )
                return SubmitCodeResponse(res)
            except NeedCaptcha as e:
                if capture_handler is None:
                    raise NeedCaptcha("Need captcha")
                logger.warning(f"({attempt}/{self.max_retries}) Raise User-defined captcha handler")
                captcha = await self._get_captcha()
                logger.debug(f"Captcha: {captcha}")
                captcha_text = capture_handler(captcha, attempt)
                await asyncio.sleep(5)
                continue
        raise RequestError("Failed to submit code after multiple attempts")
    
    async def submit_code_via_openluogu(self):
        raise NotImplementedError
    
    async def get_record(self, rid: str) -> RecordRequestResponse:
        res = await self._send_request(endpoint=record_endpoint(rid))
        return RecordRequestResponse(res)

    async def get_paste(self, id: str) -> PasteRequestResponse:
        res = await self._send_request(endpoint=paste_endpoint(id))
        return PasteRequestResponse(res)

    async def get_article(self, lid: str) -> ArticleDataRequestResponse:
        res = await self._send_request(endpoint=article_endpoint(lid))
        return ArticleDataRequestResponse(res)

    async def get_tags(self) -> TagRequestResponse:
        res = await self._send_request(endpoint=TAGS_ENDPOINT)
        return TagRequestResponse(res)

    async def get_image(self, id: int) -> Image:
        res = await self._send_request(endpoint=image_endpoint(id))
        return Image(res["image"])

    async def _request_route(
            self,
            route_name: str,
            method: str = "GET",
            path_params: dict[str, object] | None = None,
            params: RequestParams | None = None,
            data: JsonMapping | None = None,
            form: JsonMapping | None = None,
            response_type: ResponseType = "json",
    ) -> RawDataResponse:
        res = await self._send_request(
            endpoint=api_route(route_name, **(path_params or {})),
            method=method,
            params=params,
            data=_json_payload(data),
            form=_json_payload(form),
            response_type=response_type,
        )
        if response_type != "json":
            return res
        return RawDataResponse(raw_response(res))

    async def _typed_route(
            self,
            route_name: str,
            response_cls: type[_TResponse],
            method: str = "GET",
            path_params: dict[str, object] | None = None,
            params: RequestParams | None = None,
            data: JsonMapping | None = None,
            form: JsonMapping | None = None,
            normalizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> _TResponse:
        res = await self._send_request(
            endpoint=api_route(route_name, **(path_params or {})),
            method=method,
            params=params,
            data=_json_payload(data),
            form=_json_payload(form),
        )
        if normalizer is not None:
            res = normalizer(res)
        return response_cls(res)

    async def get_watching_activities(self, page: int | None = None) -> ActivityRequestResponse:
        return await self._typed_route("feed_watching", ActivityRequestResponse, params=raw_params(page=page), normalizer=normalize_activity)

    async def post_activity(self, content: str) -> RawDataResponse:
        return await self._request_route("feed_post", method="POST", form={"content": content})

    async def delete_activity(self, id: int | str) -> RawDataResponse:
        return await self._request_route("feed_delete", method="POST", path_params={"id": id})

    async def report_activity(self, id: int | str, reason: str | None = None) -> RawDataResponse:
        return await self._request_route("feed_report", method="POST", data={"id": id, "reason": reason})

    async def get_article_list(self, page: int | None = None, keyword: str | None = None) -> ArticleListRequestResponse:
        return await self._typed_route("article_list", ArticleListRequestResponse, params=raw_params(page=page, keyword=keyword), normalizer=normalize_articles)

    async def find_article(self, keyword: str, page: int | None = None) -> ArticleListRequestResponse:
        return await self._typed_route("article_find", ArticleListRequestResponse, params=raw_params(keyword=keyword, page=page), normalizer=normalize_articles)

    async def get_my_articles(self, page: int | None = None) -> ArticleListRequestResponse:
        return await self._typed_route("article_mine", ArticleListRequestResponse, params=raw_params(page=page), normalizer=normalize_articles)

    async def get_favored_articles(self, page: int | None = None) -> ArticleListRequestResponse:
        return await self._typed_route("article_favored", ArticleListRequestResponse, params=raw_params(page=page), normalizer=normalize_articles)

    async def get_article_collection(self, id: int | str, page: int | None = None) -> ArticleListRequestResponse:
        return await self._typed_route("article_collection", ArticleListRequestResponse, path_params={"id": id}, params=raw_params(page=page), normalizer=normalize_articles)

    async def get_article_available_collections(self, lid: int | str) -> RawDataResponse:
        return await self._request_route("article_available_collection", path_params={"lid": lid})

    async def create_article(self, request: EditArticleRequest) -> RawDataResponse:
        return await self._request_route("article_new", method="POST", data=request)

    async def update_article(self, lid: int | str, request: EditArticleRequest) -> RawDataResponse:
        return await self._request_route("article_edit", method="POST", path_params={"lid": lid}, data=request)

    async def delete_article(self, lid: int | str) -> RawDataResponse:
        return await self._request_route("article_delete", method="POST", path_params={"lid": lid})

    async def batch_update_articles(self, request: BatchEditArticleRequest) -> RawDataResponse:
        return await self._request_route("article_batch_edit", method="POST", data=request)

    async def favor_article(self, lid: int | str, undo: bool | None = None) -> RawDataResponse:
        return await self._request_route("article_favor", method="POST", path_params={"lid": lid}, data={"undo": undo})

    async def vote_article(self, lid: int | str, vote: int) -> RawDataResponse:
        return await self._request_route("article_vote", method="POST", path_params={"lid": lid}, params=raw_params(vote=vote))

    async def request_article_promotion(self, lid: int | str) -> RawDataResponse:
        return await self._request_route("article_request_promotion", method="POST", path_params={"lid": lid})

    async def withdraw_article_promotion(self, lid: int | str) -> RawDataResponse:
        return await self._request_route("article_withdraw_promotion", method="POST", path_params={"lid": lid})

    async def get_article_replies(self, lid: int | str, page: int | None = None) -> ArticleReplyListResponse:
        return await self._typed_route("article_replies", ArticleReplyListResponse, path_params={"lid": lid}, params=raw_params(page=page), normalizer=normalize_article_replies)

    async def reply_article(self, lid: int | str, content: str) -> RawDataResponse:
        return await self._request_route("article_reply", method="POST", path_params={"lid": lid}, data={"content": content})

    async def delete_article_reply(self, lid: int | str, id: int | str) -> RawDataResponse:
        return await self._request_route("article_delete_reply", method="POST", path_params={"lid": lid, "id": id})

    async def get_lg4_captcha(self) -> bytes:
        return await self._send_request(endpoint=api_route("captcha_lg4"), response_type="bytes")

    async def get_motp_target(self) -> RawDataResponse:
        return await self._request_route("auth_motp_to")

    async def finish_signup(self, request: RegisterRequest) -> RawDataResponse:
        return await self._request_route("auth_finish_signup", method="POST", data=request)

    async def auth_with_password(self, username: str, password: str) -> RawDataResponse:
        return await self._request_route("auth_password", method="POST", data={"username": username, "password": password})

    async def connect_openid(self, id: int | str, request: OpenIdAuthRequest | None = None) -> RawDataResponse:
        return await self._request_route("openid_connect", method="POST", path_params={"id": id}, data=request)

    async def lock_auth(self) -> RawDataResponse:
        return await self._request_route("auth_lock", method="POST")

    async def auth_with_totp(self, code: str) -> RawDataResponse:
        return await self._request_route("auth_totp", method="POST", data={"code": code})

    async def request_motp(self) -> RawDataResponse:
        return await self._request_route("auth_motp_request", method="POST")

    async def auth_with_motp(self, code: str) -> RawDataResponse:
        return await self._request_route("auth_motp", method="POST", data={"code": code})

    async def unlock_auth(self, request: AuthUnlockRequest | None = None) -> RawDataResponse:
        return await self._request_route("auth_unlock", method="POST", data=request)

    async def get_user_blogs(self, uid: int, page: int | None = None) -> BlogListRequestResponse:
        return await self._typed_route("blog_user_blogs", BlogListRequestResponse, params=raw_params(uid=uid, page=page), normalizer=normalize_blogs)

    async def get_blog_list(self, page: int | None = None, keyword: str | None = None) -> BlogListRequestResponse:
        return await self._typed_route("blog_lists", BlogListRequestResponse, params=raw_params(page=page, keyword=keyword), normalizer=normalize_blogs)

    async def get_blog(self, id: int | str) -> BlogDataRequestResponse:
        return await self._typed_route("blog_detail", BlogDataRequestResponse, path_params={"id": id}, normalizer=normalize_blog)

    async def create_blog(self, request: EditBlogRequest) -> RawDataResponse:
        return await self._request_route("blog_new", method="POST", data=request)

    async def update_blog(self, id: int | str, request: EditBlogRequest) -> RawDataResponse:
        return await self._request_route("blog_edit", method="POST", path_params={"id": id}, data=request)

    async def delete_blog(self, id: int | str) -> RawDataResponse:
        return await self._request_route("blog_delete", method="POST", path_params={"id": id})

    async def get_blog_admin_list(self, page_type: str | None = None, page: int | None = None) -> BlogListRequestResponse:
        return await self._typed_route("blog_admin_list", BlogListRequestResponse, params=raw_params(pageType=page_type, page=page), normalizer=normalize_blogs)

    async def update_blog_admin_list(self, form: BlogAdminForm, page_type: str | None = None) -> str:
        return await self._send_request(
            endpoint=api_route("blog_admin_list"),
            method="POST",
            params=raw_params(pageType=page_type),
            form=_json_payload(form),
            response_type="text",
        )

    async def get_blog_replies(self, id: int | str, page: int | None = None) -> BlogReplyListResponse:
        return await self._typed_route("blog_replies", BlogReplyListResponse, path_params={"id": id}, params=raw_params(page=page), normalizer=normalize_blog_replies)

    async def reply_blog(self, id: int | str, content: str) -> RawDataResponse:
        return await self._request_route("blog_reply", method="POST", path_params={"id": id}, data={"content": content})

    async def vote_blog(self, id: int | str, vote: int) -> RawDataResponse:
        return await self._request_route("blog_vote", method="POST", path_params={"id": id}, params=raw_params(vote=vote))

    async def delete_blog_comment(self, id: int | str) -> RawDataResponse:
        return await self._request_route("blog_delete_comment", method="POST", path_params={"id": id})

    async def get_chat_page(self) -> str:
        return await self._send_request(endpoint=api_route("chat"), response_type="text")

    async def get_chat_records(self, user: int | None = None, page: int | None = None) -> ChatRecordRequestResponse:
        return await self._typed_route("chat_record", ChatRecordRequestResponse, params=raw_params(user=user, page=page), normalizer=normalize_chat_records)

    async def create_chat(self, uid: int, content: str) -> RawDataResponse:
        return await self._request_route("chat_new", method="POST", data={"uid": uid, "content": content})

    async def delete_chat(self, id: int | str) -> RawDataResponse:
        return await self._request_route("chat_delete", method="POST", data={"id": id})

    async def clear_chat_unread(self, uid: int | None = None) -> RawDataResponse:
        return await self._request_route("chat_clear_unread", method="POST", data={"uid": uid})

    async def get_joined_contest_list(self, page: int | None = None) -> ContestListRequestResponse:
        return await self._typed_route("joined_contests", ContestListRequestResponse, params=raw_params(page=page), normalizer=normalize_contest_list)

    async def get_contest_scoreboard(self, id: int | str, page: int | None = None) -> RawDataResponse:
        return await self._request_route("contest_scoreboard", path_params={"id": id}, params=raw_params(page=page))

    async def join_contest(self, id: int | str, request: ContestJoinRequest | None = None) -> RawDataResponse:
        return await self._request_route("contest_join", method="POST", path_params={"id": id}, data=request)

    async def get_contest_squad(self, id: int | str) -> RawDataResponse:
        return await self._request_route("contest_squad", path_params={"id": id})

    async def quit_contest_squad_member(self, id: int | str, uid: int | None = None) -> RawDataResponse:
        return await self._request_route("contest_squad_member_quit", method="POST", path_params={"id": id}, data={"uid": uid})

    async def create_contest(self, request: EditContestRequest) -> RawDataResponse:
        return await self._request_route("contest_new", method="POST", data=request)

    async def update_contest(self, id: int | str, request: EditContestRequest) -> RawDataResponse:
        return await self._request_route("contest_edit", method="POST", path_params={"id": id}, data=request)

    async def update_contest_problem(self, id: int | str, request: EditContestProblemRequest) -> RawDataResponse:
        return await self._request_route("contest_edit_problem", method="POST", path_params={"id": id}, data=request)

    async def delete_contest(self, id: int | str) -> RawDataResponse:
        return await self._request_route("contest_delete", method="POST", path_params={"id": id})

    async def get_discussion_list(self, page: int | None = None, keyword: str | None = None) -> RawDataResponse:
        return await self._request_route("discuss_list", params=raw_params(page=page, keyword=keyword))

    async def get_created_post_list(self, page: int | None = None) -> RawDataResponse:
        return await self._request_route("created_posts", params=raw_params(page=page))

    async def create_discussion(self, request: CreatePostRequest) -> RawDataResponse:
        return await self._request_route("discuss_post", method="POST", data=request)

    async def reply_discussion(self, id: int | str, content: str) -> RawDataResponse:
        return await self._request_route("discuss_reply", method="POST", path_params={"id": id}, data={"content": content})

    async def delete_discussion(self, id: int | str) -> RawDataResponse:
        return await self._request_route("discuss_delete", method="POST", path_params={"id": id})

    async def delete_discussion_reply(self, id: int | str) -> RawDataResponse:
        return await self._request_route("discuss_delete_reply", method="POST", path_params={"id": id})

    async def report_post(self, id: int | str, reason: str | None = None) -> RawDataResponse:
        return await self._request_route("post_report", method="POST", data={"id": id, "reason": reason})

    async def report_post_reply(self, id: int | str, reason: str | None = None) -> RawDataResponse:
        return await self._request_route("post_reply_report", method="POST", data={"id": id, "reason": reason})

    async def submit_ide_code(self, code: str, language: int, input_data: str | None = None) -> RawDataResponse:
        return await self._request_route("ide_submit", method="POST", data={"code": code, "lang": language, "input": input_data})

    async def get_image_list(self, page: int | None = None) -> ImageListRequestResponse:
        return await self._typed_route("image_list", ImageListRequestResponse, params=raw_params(page=page), normalizer=normalize_images)

    async def generate_image_upload_link(self, request: GenerateUploadLinkRequest | None = None) -> GenerateUploadLinkResponse:
        return await self._typed_route("image_generate_upload_link", GenerateUploadLinkResponse, method="POST", data=request)

    async def delete_image(self, id: int | str) -> RawDataResponse:
        return await self._request_route("image_delete", method="POST", data={"id": id})

    async def get_config(self) -> RawDataResponse:
        return await self._request_route("config")

    async def get_ranking(self, page: int | None = None) -> RankingListRequestResponse:
        return await self._typed_route("ranking", RankingListRequestResponse, params=raw_params(page=page), normalizer=normalize_rankings)

    async def get_elo_ranking(self, page: int | None = None) -> RankingListRequestResponse:
        return await self._typed_route("ranking_elo", RankingListRequestResponse, params=raw_params(page=page), normalizer=normalize_rankings)

    async def get_notifications(self, page: int | None = None) -> NotificationListRequestResponse:
        return await self._typed_route("notification", NotificationListRequestResponse, params=raw_params(page=page), normalizer=normalize_notifications)

    async def get_advertisement(self, id: int | str) -> RawDataResponse:
        return await self._request_route("advertisement", path_params={"id": id})

    async def get_paintboard(self) -> RawDataResponse:
        return await self._request_route("paintboard_board")

    async def reset_paintboard_token(self) -> RawDataResponse:
        return await self._request_route("paintboard_reset_token", method="POST")

    async def paint(self, x: int, y: int, color: int) -> RawDataResponse:
        return await self._request_route("paintboard_paint", method="POST", data={"x": x, "y": y, "color": color})

    async def get_paste_list(self, page: int | None = None) -> PasteListRequestResponse:
        return await self._typed_route("paste_list", PasteListRequestResponse, params=raw_params(page=page), normalizer=normalize_pastes)

    async def create_paste(self, request: EditPasteRequest) -> RawDataResponse:
        return await self._request_route("paste_new", method="POST", data=request)

    async def update_paste(self, id: int | str, request: EditPasteRequest) -> RawDataResponse:
        return await self._request_route("paste_edit", method="POST", path_params={"id": id}, data=request)

    async def delete_paste(self, id: int | str) -> RawDataResponse:
        return await self._request_route("paste_delete", method="POST", path_params={"id": id})

    async def get_marked_training_list(self, page: int | None = None) -> ProblemSetListRequestResponse:
        return await self._typed_route("marked_trainings", ProblemSetListRequestResponse, params=raw_params(page=page), normalizer=normalize_problem_set_list)

    async def mark_training(self, id: int | str) -> RawDataResponse:
        return await self._request_route("training_mark", method="POST", path_params={"id": id})

    async def unmark_training(self, id: int | str) -> RawDataResponse:
        return await self._request_route("training_unmark", method="POST", path_params={"id": id})

    async def create_training(self, request: EditTrainingRequest) -> RawDataResponse:
        return await self._request_route("training_new", method="POST", data=request)

    async def update_training(self, id: int | str, request: EditTrainingRequest) -> RawDataResponse:
        return await self._request_route("training_edit", method="POST", path_params={"id": id}, data=request)

    async def add_training_problem(self, id: int | str, pid: str) -> RawDataResponse:
        return await self._request_route("training_add_problem", method="POST", path_params={"id": id}, data={"pid": pid})

    async def update_training_problems(self, id: int | str, request: EditTrainingProblemsRequest) -> RawDataResponse:
        return await self._request_route("training_edit_problems", method="POST", path_params={"id": id}, data=request)

    async def clone_training(self, id: int | str) -> RawDataResponse:
        return await self._request_route("training_clone", method="POST", path_params={"id": id})

    async def delete_training(self, id: int | str) -> RawDataResponse:
        return await self._request_route("training_delete", method="POST", path_params={"id": id})

    async def add_problem_to_tasklist(self, pid: str) -> RawDataResponse:
        return await self._request_route("problem_tasklist_add", method="POST", data={"pid": pid})

    async def remove_problem_from_tasklist(self, pid: str) -> RawDataResponse:
        return await self._request_route("problem_tasklist_remove", method="POST", data={"pid": pid})

    async def translate_problem(self, pid: str, request: TranslateProblemRequest | None = None) -> RawDataResponse:
        return await self._request_route("problem_translate", method="POST", path_params={"pid": pid}, data=request)

    async def get_record_list(
            self,
            page: int | None = None,
            uid: int | None = None,
            pid: str | None = None,
            contestId: int | None = None,
            user: str | None = None,
            status: int | None = None,
            language: int | None = None,
            orderBy: int | None = None,
    ) -> RecordListRequestResponse:
        if user is None and uid is not None:
            user = str(uid)
        return await self._typed_route(
            "record_list",
            RecordListRequestResponse,
            params=raw_params(
                page=page,
                uid=uid,
                pid=pid,
                contestId=contestId,
                user=user,
                status=status,
                language=language,
                orderBy=orderBy,
            ),
            normalizer=normalize_records,
        )

    async def query_downloadable_testcase(self, id: int | str) -> DownloadableTestcaseResponse:
        return await self._typed_route("record_downloadable_testcase", DownloadableTestcaseResponse, path_params={"id": id}, normalizer=normalize_downloadable_testcases)

    async def download_record_testcase(self, id: int | str, testcase: int | str | None = None) -> bytes:
        return await self._send_request(
            endpoint=api_route("record_download_testcase", id=id),
            params=raw_params(testcase=testcase),
            response_type="bytes",
        )

    async def get_my_teams(self) -> TeamListRequestResponse:
        return await self._typed_route("mine_team", TeamListRequestResponse, normalizer=normalize_teams)

    async def join_team(self, id: int | str, request: TeamJoinRequest | None = None) -> RawDataResponse:
        return await self._request_route("team_join", method="POST", path_params={"id": id}, data=request)

    async def exit_team(self, id: int | str) -> RawDataResponse:
        return await self._request_route("team_exit", method="POST", path_params={"id": id})

    async def create_team(self, request: EditTeamRequest) -> RawDataResponse:
        return await self._request_route("team_create", method="POST", data=request)

    async def update_team(self, id: int | str, request: EditTeamRequest) -> RawDataResponse:
        return await self._request_route("team_edit", method="POST", path_params={"id": id}, data=request)

    async def set_team_master(self, id: int | str, uid: int) -> RawDataResponse:
        return await self._request_route("team_set_master", method="POST", path_params={"id": id}, data={"uid": uid})

    async def update_team_notice(self, id: int | str, content: str) -> RawDataResponse:
        return await self._request_route("team_edit_notice", method="POST", path_params={"id": id}, data={"content": content})

    async def update_team_member(self, id: int | str, uid: int, request: TeamMemberUpdateRequest) -> RawDataResponse:
        return await self._request_route("team_edit_member", method="POST", path_params={"id": id}, data={"uid": uid, **request})

    async def review_team_join_request(self, id: int | str, uid: int, accepted: bool) -> RawDataResponse:
        return await self._request_route("team_review", method="POST", path_params={"id": id}, data={"uid": uid, "accepted": accepted})

    async def kick_team_member(self, id: int | str, uid: int) -> RawDataResponse:
        return await self._request_route("team_kick", method="POST", path_params={"id": id}, data={"uid": uid})

    async def get_theme_list(self, page: int | None = None) -> ThemeListRequestResponse:
        return await self._typed_route("theme_list", ThemeListRequestResponse, params=raw_params(page=page), normalizer=normalize_themes)

    async def get_theme_design(self, id: int | str) -> RawDataResponse:
        return await self._request_route("theme_design", path_params={"id": id})

    async def set_theme(self, id: int | str) -> RawDataResponse:
        return await self._request_route("theme_set", method="POST", path_params={"id": id})

    async def create_theme(self, request: EditThemeRequest) -> RawDataResponse:
        return await self._request_route("theme_new", method="POST", data=request)

    async def update_theme(self, id: int | str, request: EditThemeRequest) -> RawDataResponse:
        return await self._request_route("theme_edit", method="POST", path_params={"id": id}, data=request)

    async def delete_theme(self, id: int | str) -> RawDataResponse:
        return await self._request_route("theme_delete", method="POST", path_params={"id": id})

    async def get_user_practice(self, uid: int) -> UserPracticeResponse:
        return await self._typed_route("user_practice", UserPracticeResponse, path_params={"uid": uid}, normalizer=normalize_user_practice)

    async def get_rating_elo(self, uid: int | None = None) -> RawDataResponse:
        return await self._request_route("rating_elo", params=raw_params(uid=uid))

    async def get_user_setting(self) -> UserSettingResponse:
        return await self._typed_route("user_setting", UserSettingResponse, normalizer=raw_response)

    async def get_user_preference(self) -> UserSettingResponse:
        return await self._typed_route("user_preference", UserSettingResponse, normalizer=raw_response)

    async def update_user_preference(self, request: UserPreferenceUpdateRequest) -> RawDataResponse:
        return await self._request_route("user_preference_update", method="POST", data=request)

    async def get_user_prize_setting(self) -> UserSettingResponse:
        return await self._typed_route("user_prize_setting", UserSettingResponse, normalizer=raw_response)

    async def get_user_security_setting(self) -> UserSettingResponse:
        return await self._typed_route("user_security_setting", UserSettingResponse, normalizer=raw_response)

    async def update_user_slogan(self, slogan: str) -> RawDataResponse:
        return await self._request_route("user_update_slogan", method="POST", data={"slogan": slogan})

    async def update_user_introduction(self, introduction: str) -> RawDataResponse:
        return await self._request_route("user_update_introduction", method="POST", data={"introduction": introduction})

    async def update_user_header_image(self, image: str) -> RawDataResponse:
        return await self._request_route("user_update_header_image", method="POST", data={"image": image})

    async def bind_vjudge_account(self, request: BindRemoteJudgeAccountRequest) -> RawDataResponse:
        return await self._request_route("user_bind_vjudge", method="POST", data=request)

    async def unbind_vjudge_account(self) -> RawDataResponse:
        return await self._request_route("user_unbind_vjudge", method="POST")

    async def bind_openid(self, id: int | str, request: OpenIdAuthRequest | None = None) -> RawDataResponse:
        return await self._request_route("openid_bind", method="POST", path_params={"id": id}, data=request)

    async def unbind_openid(self, id: int | str) -> RawDataResponse:
        return await self._request_route("user_unbind_openid", method="POST", path_params={"id": id})
