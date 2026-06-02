from typing import Any, List, Literal, Callable, TypeVar, cast

import httpx

from .types import *
from .errors import *
from .api_helpers import *
from .normalizers import *
from .transport import SyncLuoguTransportMixin

_TResponse = TypeVar("_TResponse", bound=Response)


def _json_payload(payload: JsonMapping | None) -> JsonObject | None:
    if payload is None:
        return None
    return cast(JsonObject, dict(payload))


class luoguAPI(SyncLuoguTransportMixin):
    def __init__(
            self,
            base_url="https://www.luogu.com.cn",
            cookies: LuoguCookies | None = None,
            timeout: float | httpx.Timeout | None = 10,
            max_retries: int = 5,
    ):
        self._init_transport(base_url, cookies, max_retries)
        self.client = httpx.Client(
            timeout=timeout,
            cookies=self.cookies,
            follow_redirects=True,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False

    def _post_captcha(self, captcha: str):
        raise NotImplementedError
    
    def login(
            self, user_name: str, password: str,
            captcha: Literal["input", "ocr"],
            two_step_verify: Literal["google", "email"] | None = None
    ) -> bool:
        raise NotImplementedError

    def logout(self):
        res = self._send_request(endpoint=api_route("auth_logout"), method="POST")
        self.x_csrf_token = None
        return EmptyResponse(empty_response(res))

    def get_problem_list(
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
        res = self._send_request(endpoint=PROBLEM_LIST_ENDPOINT, params=params)

        return ProblemListRequestResponse(normalize_problem_list(res))

    def get_problem(
            self, pid: str,
            contest_id: int | None = None
    ) -> ProblemDataRequestResponse:
        params = problem_request_params(contest_id)
        res = self._send_request(endpoint=problem_endpoint(pid), params=params)

        return ProblemDataRequestResponse(normalize_problem_data(res))

    def get_problem_settings_legacy(
            self, pid: str,
    ) -> ProblemSettingsRequestResponse:
        res = self._send_request(endpoint=problem_settings_legacy_endpoint(pid))
        
        return ProblemSettingsRequestResponse(normalize_problem_settings_legacy(res))

    def get_problem_settings(self, pid: str) -> ProblemSettingsRequestResponse:
        res = self._send_request(endpoint=problem_settings_endpoint(pid))
        
        return ProblemSettingsRequestResponse(normalize_problem_settings(res))

    def update_problem_settings(
            self, pid: str,
            new_settings: ProblemSettings,
    ) -> ProblemModifiedResponse:
        res = self._send_request(
            endpoint=problem_edit_endpoint(pid),
            method="POST",
            data=update_problem_payload(new_settings)
        )

        return ProblemModifiedResponse(res)

    def update_testcases_settings(
            self, pid: str,
            new_settings: TestCaseSettings
    ) -> UpdateTestCasesSettingsResponse:
        res = self._send_request(
            endpoint=problem_edit_testcase_endpoint(pid),
            method="POST",
            data=new_settings.to_json()
        )

        return UpdateTestCasesSettingsResponse(res)

    def create_problem(
            self, settings: ProblemSettings,
            tid : int | None = None,

    ) -> ProblemModifiedResponse:
        _type = "U" if tid is None else "T"
        res = self._send_request(
            endpoint=PROBLEM_NEW_ENDPOINT,
            method="POST",
            data=create_problem_payload(settings, _type, tid)
        )

        return ProblemModifiedResponse(res)

    def delete_problem(
            self, pid: str,
    ) -> bool:
        res = self._send_request(
            endpoint=problem_delete_endpoint(pid),
            method="POST",
            data={}
        )

        return res["_empty"]

    def transfer_problem(
            self, pid: str,
            target: TransferProblemType = "U",
            is_clone: bool = False
    ) -> ProblemModifiedResponse:
        res = self._send_request(
            endpoint=problem_transfer_endpoint(pid),
            method="POST",
            data=transfer_problem_payload(target, is_clone)
        )

        return ProblemModifiedResponse(res)

    def download_testcases(
            self, pid: int
    ):
        raise NotImplementedError
    
    def upload_testcases(
            self, pid: int,
            path: str
    ):
        raise NotImplementedError

    def get_problem_solutions(self, pid: str, page: int | None = None) -> ProblemSolutionRequestResponse:
        params = list_params(page)
        res = self._send_request(endpoint=problem_solution_endpoint(pid), params=params)

        return ProblemSolutionRequestResponse(normalize_problem_solutions(res))

    def get_user(self, uid: int) -> UserDataRequestResponse:
        res = self._send_request(endpoint=user_endpoint(uid))
        
        return UserDataRequestResponse(normalize_user_data(res))

    def get_user_info(self, uid: int) -> UserDetails:
        res = self._send_request(endpoint=user_info_endpoint(uid))

        return UserDetails(res["user"])
    
    def get_user_following_list(self, uid: int, page: int | None = None) -> List[UserDetails]:
        params = user_list_params(uid, page)
        res = self._send_request(endpoint=user_followings_endpoint(), params=params)
        return [UserDetails(user) for user in res["users"]["result"]]

    def get_user_follower_list(self, uid: int, page: int | None = None) -> List[UserDetails]:
        params = user_list_params(uid, page)
        res = self._send_request(endpoint=user_followers_endpoint(), params=params)
        return [UserDetails(user) for user in res["users"]["result"]]

    def get_user_blacklist(self, uid: int, page: int | None = None) -> List[UserDetails]:
        params = user_list_params(uid, page)
        res = self._send_request(endpoint=user_blacklist_endpoint(), params=params)
        return [UserDetails(user) for user in res["users"]["result"]]
    
    def search_user(self, keyword: str) -> List[UserSummary]:
        params = UserSearchRequestParams({"keyword" : keyword})
        
        res = self._send_request(endpoint=USER_SEARCH_ENDPOINT, params=params)
        return [UserSummary(user) for user in res["users"]]

    def me(self) -> UserDetails:
        if self.cookies is None or "_uid" not in self.cookies:
            raise AuthenticationError("Need Login")
        return self.get_user(int(self.cookies["_uid"].split("_")[0])).user

    def get_problem_set(self, id: int) -> ProblemSetDataRequestResponse:
        res = self._send_request(endpoint=problem_set_endpoint(id))
        return ProblemSetDataRequestResponse(normalize_problem_set(res))
    
    def get_problem_set_list(
            self,
            page: int | None = None,
            keyword: str | None = None,
            type: ProblemSetType | None = None, 
            params: ProblemSetListRequestParams | None = None
    ):
        if params is None:
            params = problem_set_list_params(page, keyword, type)
        res = self._send_request(endpoint=PROBLEM_SET_LIST_ENDPOINT, params=params)
        return ProblemSetListRequestResponse(normalize_problem_set_list(res))
    
    def get_contest(self, id: int) -> ContestDataRequestResponse:
        res = self._send_request(endpoint=contest_endpoint(id))

        return ContestDataRequestResponse(normalize_contest(res))
    
    def get_contest_list(
            self,
            page: int | None = None,
            name: str | None = None,
            method: int | None = None,
            public: int | None = None,
    ) -> ContestListRequestResponse:
        params = contest_list_params(page, name, method, public)
        res = self._send_request(endpoint=CONTEST_LIST_ENDPOINT, params=params)
        return ContestListRequestResponse(normalize_contest_list(res))
            
    def get_disscussion(self,
            id: int,
            page: int | None = None,
            orderBy: int | None = None,
    ) -> DiscussionRequestResponse:
        params = discussion_params(page, orderBy)
        res = self._send_request(endpoint=discussion_endpoint(id), params=params)

        return DiscussionRequestResponse(normalize_discussion(res))     
    
    def get_activity(self, 
            uid: int, 
            page: int | None = None
    ) -> ActivityRequestResponse:
        params = activity_params(uid, page)
        res = self._send_request(endpoint=ACTIVITY_ENDPOINT, params=params)

        return ActivityRequestResponse(normalize_activity(res))

    def get_team(self, tid: int) -> TeamDataRequestResponse:
        res = self._send_request(endpoint=team_endpoint(tid))
        return TeamDataRequestResponse(res)

    def get_team_member_list(self, tid: int) -> TeamMemberRequestResponse:
        res = self._send_request(endpoint=team_members_endpoint(tid))
        return TeamMemberRequestResponse(normalize_team_members(res))

    def get_team_problem_list(
            self, tid: int,
            page: int | None = None,
            keyword: str | None = None,
            orderBy: Literal["pid", "name"] | None = None,
            order: Literal["asc", "desc"] | None = None,
    ) -> ProblemListRequestResponse:
        params = team_problem_list_params(page, keyword, orderBy, order)
        res = self._send_request(
            endpoint=team_problems_endpoint(tid), 
            params=params
        )

        return ProblemListRequestResponse(normalize_problem_list(res))

    def get_team_problem_set_list(self, tid: int, page: int | None = None) -> ProblemSetListRequestResponse:
        params = list_params(page)
        res = self._send_request(endpoint=team_problem_sets_endpoint(tid), params=params)
        return ProblemSetListRequestResponse(normalize_problem_set_list(res))
    
    def get_team_contest_list(self, tid: int, page: int | None = None) -> ContestListRequestResponse:
        params = list_params(page)
        res = self._send_request(endpoint=team_contests_endpoint(tid), params=params)
        return ContestListRequestResponse(normalize_contest_list(res))

    def get_created_contest(self, id: int) -> RawDataResponse:
        return self._request_route("contest_created", path_params={"id": id})

    def get_team_member_page(self, tid: int, page: int | None = None) -> TeamMemberRequestResponse:
        return self._typed_route(
            "team_member_page",
            TeamMemberRequestResponse,
            path_params={"id": tid},
            params=raw_params(page=page),
            normalizer=normalize_team_members,
        )

    def get_team_problem_page(
            self,
            tid: int,
            page: int | None = None,
            keyword: str | None = None,
            orderBy: Literal["pid", "name"] | None = None,
            order: Literal["asc", "desc"] | None = None,
    ) -> ProblemListRequestResponse:
        return self._typed_route(
            "team_problem_page",
            ProblemListRequestResponse,
            path_params={"id": tid},
            params=team_problem_list_params(page, keyword, orderBy, order),
            normalizer=normalize_problem_list,
        )

    def get_team_training_page(self, tid: int, page: int | None = None) -> ProblemSetListRequestResponse:
        return self._typed_route(
            "team_training_page",
            ProblemSetListRequestResponse,
            path_params={"id": tid},
            params=list_params(page),
            normalizer=normalize_problem_set_list,
        )

    def get_team_contest_page(self, tid: int, page: int | None = None) -> ContestListRequestResponse:
        return self._typed_route(
            "team_contest_page",
            ContestListRequestResponse,
            path_params={"id": tid},
            params=list_params(page),
            normalizer=normalize_contest_list,
        )

    def get_paste(self, id: str) -> PasteRequestResponse:
        res = self._send_request(endpoint=paste_endpoint(id))
        return PasteRequestResponse(res)

    def get_record(self, rid: str) -> RecordRequestResponse:
        res = self._send_request(endpoint=record_endpoint(rid))
        return RecordRequestResponse(res)
    
    def get_article(self, lid: str) -> ArticleDataRequestResponse:
        res = self._send_request(endpoint=article_endpoint(lid))
        return ArticleDataRequestResponse(res)
    
    def get_created_problem_list(
            self, page: int | None = None
    ) -> ProblemListRequestResponse:
        params = list_params(page)
        res = self._send_request(endpoint=CREATED_PROBLEMS_ENDPOINT, params=params)

        return ProblemListRequestResponse(normalize_problem_list(res))

    def get_created_problem_set_list(self, page: int | None = None) -> ProblemSetListRequestResponse:
        params = list_params(page)
        res = self._send_request(endpoint=CREATED_PROBLEM_SETS_ENDPOINT, params=params)

        return ProblemSetListRequestResponse(normalize_problem_set_list(res))
    
    def get_created_contest_list(self, page: int | None = None) -> ContestListRequestResponse:
        params = list_params(page)
        res = self._send_request(endpoint=CREATED_CONTESTS_ENDPOINT, params=params)
        return ContestListRequestResponse(normalize_contest_list(res))

    def submit_code(
            self,
            pid: str,
            code: str,
            contest_id: int | None = None,
            lang: int | None = None,
            enableO2: bool | int = True,
            capture_handler: Callable[[bytes], str] | None = None
    ) -> SubmitCodeResponse:
        captcha_text = ""
        for attempt in range(self.max_retries):
            try:
                self._get_csrf(f"/problem/{pid}")
                res = self._send_request(
                    endpoint=problem_submit_endpoint(pid),
                    params=problem_request_params(contest_id),
                    method="POST",
                    data=submit_code_payload(code, lang, enableO2, captcha_text)
                )
                return SubmitCodeResponse(res)
            except NeedCaptcha:
                if capture_handler is None:
                    raise NeedCaptcha("Need captcha")
                captcha = self._get_captcha()
                captcha_text = capture_handler(captcha)
        
        raise RequestError("Failed to submit code after multiple attempts")

    def submit_code_via_openluogu(self):
        raise NotImplementedError
    
    def get_tags(self) -> TagRequestResponse:
        res = self._send_request(endpoint=TAGS_ENDPOINT)
        return TagRequestResponse(res)

    def get_image(self, id: int) -> Image:
        res = self._send_request(endpoint=image_endpoint(id))
        return Image(res["image"])

    def _request_route(
            self,
            route_name: str,
            method: str = "GET",
            path_params: dict[str, object] | None = None,
            params: RequestParams | None = None,
            data: JsonMapping | None = None,
            form: JsonMapping | None = None,
            response_type: ResponseType = "json",
    ) -> RawDataResponse:
        res = self._send_request(
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

    def _typed_route(
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
        res = self._send_request(
            endpoint=api_route(route_name, **(path_params or {})),
            method=method,
            params=params,
            data=_json_payload(data),
            form=_json_payload(form),
        )
        if normalizer is not None:
            res = normalizer(res)
        return response_cls(res)

    def get_watching_activities(self, page: int | None = None) -> ActivityRequestResponse:
        return self._typed_route("feed_watching", ActivityRequestResponse, params=raw_params(page=page), normalizer=normalize_activity)

    def post_activity(self, content: str) -> RawDataResponse:
        return self._request_route("feed_post", method="POST", form={"content": content})

    def delete_activity(self, id: int | str) -> RawDataResponse:
        return self._request_route("feed_delete", method="POST", path_params={"id": id})

    def report_activity(self, id: int | str, reason: str | None = None) -> RawDataResponse:
        return self._request_route("feed_report", method="POST", data={"id": id, "reason": reason})

    def get_article_list(self, page: int | None = None, keyword: str | None = None) -> ArticleListRequestResponse:
        return self._typed_route("article_list", ArticleListRequestResponse, params=raw_params(page=page, keyword=keyword), normalizer=normalize_articles)

    def find_article(self, keyword: str, page: int | None = None) -> ArticleListRequestResponse:
        return self._typed_route("article_find", ArticleListRequestResponse, params=raw_params(keyword=keyword, page=page), normalizer=normalize_articles)

    def get_my_articles(self, page: int | None = None) -> ArticleListRequestResponse:
        return self._typed_route("article_mine", ArticleListRequestResponse, params=raw_params(page=page), normalizer=normalize_articles)

    def get_favored_articles(self, page: int | None = None) -> ArticleListRequestResponse:
        return self._typed_route("article_favored", ArticleListRequestResponse, params=raw_params(page=page), normalizer=normalize_articles)

    def get_article_collection(self, id: int | str, page: int | None = None) -> ArticleListRequestResponse:
        return self._typed_route("article_collection", ArticleListRequestResponse, path_params={"id": id}, params=raw_params(page=page), normalizer=normalize_articles)

    def get_article_available_collections(self, lid: int | str) -> RawDataResponse:
        return self._request_route("article_available_collection", path_params={"lid": lid})

    def create_article(self, request: EditArticleRequest) -> RawDataResponse:
        return self._request_route("article_new", method="POST", data=request)

    def update_article(self, lid: int | str, request: EditArticleRequest) -> RawDataResponse:
        return self._request_route("article_edit", method="POST", path_params={"lid": lid}, data=request)

    def delete_article(self, lid: int | str) -> RawDataResponse:
        return self._request_route("article_delete", method="POST", path_params={"lid": lid})

    def batch_update_articles(self, request: BatchEditArticleRequest) -> RawDataResponse:
        return self._request_route("article_batch_edit", method="POST", data=request)

    def favor_article(self, lid: int | str, undo: bool | None = None) -> RawDataResponse:
        return self._request_route("article_favor", method="POST", path_params={"lid": lid}, data={"undo": undo})

    def vote_article(self, lid: int | str, vote: int) -> RawDataResponse:
        return self._request_route("article_vote", method="POST", path_params={"lid": lid}, params=raw_params(vote=vote))

    def request_article_promotion(self, lid: int | str) -> RawDataResponse:
        return self._request_route("article_request_promotion", method="POST", path_params={"lid": lid})

    def withdraw_article_promotion(self, lid: int | str) -> RawDataResponse:
        return self._request_route("article_withdraw_promotion", method="POST", path_params={"lid": lid})

    def get_article_replies(self, lid: int | str, page: int | None = None) -> ArticleReplyListResponse:
        return self._typed_route("article_replies", ArticleReplyListResponse, path_params={"lid": lid}, params=raw_params(page=page), normalizer=normalize_article_replies)

    def reply_article(self, lid: int | str, content: str) -> RawDataResponse:
        return self._request_route("article_reply", method="POST", path_params={"lid": lid}, data={"content": content})

    def delete_article_reply(self, lid: int | str, id: int | str) -> RawDataResponse:
        return self._request_route("article_delete_reply", method="POST", path_params={"lid": lid, "id": id})

    def get_lg4_captcha(self) -> bytes:
        return self._send_request(endpoint=api_route("captcha_lg4"), response_type="bytes")

    def get_motp_target(self) -> RawDataResponse:
        return self._request_route("auth_motp_to")

    def finish_signup(self, request: RegisterRequest) -> RawDataResponse:
        return self._request_route("auth_finish_signup", method="POST", data=request)

    def auth_with_password(self, username: str, password: str) -> RawDataResponse:
        return self._request_route("auth_password", method="POST", data={"username": username, "password": password})

    def connect_openid(self, id: int | str, request: OpenIdAuthRequest | None = None) -> RawDataResponse:
        return self._request_route("openid_connect", method="POST", path_params={"id": id}, data=request)

    def lock_auth(self) -> RawDataResponse:
        return self._request_route("auth_lock", method="POST")

    def auth_with_totp(self, code: str) -> RawDataResponse:
        return self._request_route("auth_totp", method="POST", data={"code": code})

    def request_motp(self) -> RawDataResponse:
        return self._request_route("auth_motp_request", method="POST")

    def auth_with_motp(self, code: str) -> RawDataResponse:
        return self._request_route("auth_motp", method="POST", data={"code": code})

    def unlock_auth(self, request: AuthUnlockRequest | None = None) -> RawDataResponse:
        return self._request_route("auth_unlock", method="POST", data=request)

    def get_user_blogs(self, uid: int, page: int | None = None) -> BlogListRequestResponse:
        return self._typed_route("blog_user_blogs", BlogListRequestResponse, params=raw_params(uid=uid, page=page), normalizer=normalize_blogs)

    def get_blog_list(self, page: int | None = None, keyword: str | None = None) -> BlogListRequestResponse:
        return self._typed_route("blog_lists", BlogListRequestResponse, params=raw_params(page=page, keyword=keyword), normalizer=normalize_blogs)

    def get_blog(self, id: int | str) -> BlogDataRequestResponse:
        return self._typed_route("blog_detail", BlogDataRequestResponse, path_params={"id": id}, normalizer=normalize_blog)

    def create_blog(self, request: EditBlogRequest) -> RawDataResponse:
        return self._request_route("blog_new", method="POST", data=request)

    def update_blog(self, id: int | str, request: EditBlogRequest) -> RawDataResponse:
        return self._request_route("blog_edit", method="POST", path_params={"id": id}, data=request)

    def delete_blog(self, id: int | str) -> RawDataResponse:
        return self._request_route("blog_delete", method="POST", path_params={"id": id})

    def get_blog_admin_list(self, page_type: str | None = None, page: int | None = None) -> BlogListRequestResponse:
        return self._typed_route("blog_admin_list", BlogListRequestResponse, params=raw_params(pageType=page_type, page=page), normalizer=normalize_blogs)

    def update_blog_admin_list(self, form: BlogAdminForm, page_type: str | None = None) -> str:
        return self._send_request(
            endpoint=api_route("blog_admin_list"),
            method="POST",
            params=raw_params(pageType=page_type),
            form=_json_payload(form),
            response_type="text",
        )

    def get_blog_replies(self, id: int | str, page: int | None = None) -> BlogReplyListResponse:
        return self._typed_route("blog_replies", BlogReplyListResponse, path_params={"id": id}, params=raw_params(page=page), normalizer=normalize_blog_replies)

    def reply_blog(self, id: int | str, content: str) -> RawDataResponse:
        return self._request_route("blog_reply", method="POST", path_params={"id": id}, data={"content": content})

    def vote_blog(self, id: int | str, vote: int) -> RawDataResponse:
        return self._request_route("blog_vote", method="POST", path_params={"id": id}, params=raw_params(vote=vote))

    def delete_blog_comment(self, id: int | str) -> RawDataResponse:
        return self._request_route("blog_delete_comment", method="POST", path_params={"id": id})

    def get_chat_page(self) -> str:
        return self._send_request(endpoint=api_route("chat"), response_type="text")

    def get_chat_records(self, user: int | None = None, page: int | None = None) -> ChatRecordRequestResponse:
        return self._typed_route("chat_record", ChatRecordRequestResponse, params=raw_params(user=user, page=page), normalizer=normalize_chat_records)

    def create_chat(self, uid: int, content: str) -> RawDataResponse:
        return self._request_route("chat_new", method="POST", data={"uid": uid, "content": content})

    def delete_chat(self, id: int | str) -> RawDataResponse:
        return self._request_route("chat_delete", method="POST", data={"id": id})

    def clear_chat_unread(self, uid: int | None = None) -> RawDataResponse:
        return self._request_route("chat_clear_unread", method="POST", data={"uid": uid})

    def get_joined_contest_list(self, page: int | None = None) -> ContestListRequestResponse:
        return self._typed_route("joined_contests", ContestListRequestResponse, params=raw_params(page=page), normalizer=normalize_contest_list)

    def get_contest_scoreboard(self, id: int | str, page: int | None = None) -> RawDataResponse:
        return self._request_route("contest_scoreboard", path_params={"id": id}, params=raw_params(page=page))

    def join_contest(self, id: int | str, request: ContestJoinRequest | None = None) -> RawDataResponse:
        return self._request_route("contest_join", method="POST", path_params={"id": id}, data=request)

    def get_contest_squad(self, id: int | str) -> RawDataResponse:
        return self._request_route("contest_squad", path_params={"id": id})

    def quit_contest_squad_member(self, id: int | str, uid: int | None = None) -> RawDataResponse:
        return self._request_route("contest_squad_member_quit", method="POST", path_params={"id": id}, data={"uid": uid})

    def create_contest(self, request: EditContestRequest) -> RawDataResponse:
        return self._request_route("contest_new", method="POST", data=request)

    def update_contest(self, id: int | str, request: EditContestRequest) -> RawDataResponse:
        return self._request_route("contest_edit", method="POST", path_params={"id": id}, data=request)

    def update_contest_problem(self, id: int | str, request: EditContestProblemRequest) -> RawDataResponse:
        return self._request_route("contest_edit_problem", method="POST", path_params={"id": id}, data=request)

    def delete_contest(self, id: int | str) -> RawDataResponse:
        return self._request_route("contest_delete", method="POST", path_params={"id": id})

    def get_discussion_list(self, page: int | None = None, keyword: str | None = None) -> RawDataResponse:
        return self._request_route("discuss_list", params=raw_params(page=page, keyword=keyword))

    def get_created_post_list(self, page: int | None = None) -> RawDataResponse:
        return self._request_route("created_posts", params=raw_params(page=page))

    def create_discussion(self, request: CreatePostRequest) -> RawDataResponse:
        return self._request_route("discuss_post", method="POST", data=request)

    def reply_discussion(self, id: int | str, content: str) -> RawDataResponse:
        return self._request_route("discuss_reply", method="POST", path_params={"id": id}, data={"content": content})

    def delete_discussion(self, id: int | str) -> RawDataResponse:
        return self._request_route("discuss_delete", method="POST", path_params={"id": id})

    def delete_discussion_reply(self, id: int | str) -> RawDataResponse:
        return self._request_route("discuss_delete_reply", method="POST", path_params={"id": id})

    def report_post(self, id: int | str, reason: str | None = None) -> RawDataResponse:
        return self._request_route("post_report", method="POST", data={"id": id, "reason": reason})

    def report_post_reply(self, id: int | str, reason: str | None = None) -> RawDataResponse:
        return self._request_route("post_reply_report", method="POST", data={"id": id, "reason": reason})

    def submit_ide_code(self, code: str, language: int, input_data: str | None = None) -> RawDataResponse:
        return self._request_route("ide_submit", method="POST", data={"code": code, "lang": language, "input": input_data})

    def get_image_list(self, page: int | None = None) -> ImageListRequestResponse:
        return self._typed_route("image_list", ImageListRequestResponse, params=raw_params(page=page), normalizer=normalize_images)

    def generate_image_upload_link(self, request: GenerateUploadLinkRequest | None = None) -> GenerateUploadLinkResponse:
        return self._typed_route("image_generate_upload_link", GenerateUploadLinkResponse, method="POST", data=request)

    def delete_image(self, id: int | str) -> RawDataResponse:
        return self._request_route("image_delete", method="POST", data={"id": id})

    def get_config(self) -> RawDataResponse:
        return self._request_route("config")

    def get_ranking(self, page: int | None = None) -> RankingListRequestResponse:
        return self._typed_route("ranking", RankingListRequestResponse, params=raw_params(page=page), normalizer=normalize_rankings)

    def get_elo_ranking(self, page: int | None = None) -> RankingListRequestResponse:
        return self._typed_route("ranking_elo", RankingListRequestResponse, params=raw_params(page=page), normalizer=normalize_rankings)

    def get_notifications(self, page: int | None = None) -> NotificationListRequestResponse:
        return self._typed_route("notification", NotificationListRequestResponse, params=raw_params(page=page), normalizer=normalize_notifications)

    def get_advertisement(self, id: int | str) -> RawDataResponse:
        return self._request_route("advertisement", path_params={"id": id})

    def get_paintboard(self) -> RawDataResponse:
        return self._request_route("paintboard_board")

    def reset_paintboard_token(self) -> RawDataResponse:
        return self._request_route("paintboard_reset_token", method="POST")

    def paint(self, x: int, y: int, color: int) -> RawDataResponse:
        return self._request_route("paintboard_paint", method="POST", data={"x": x, "y": y, "color": color})

    def get_paste_list(self, page: int | None = None) -> PasteListRequestResponse:
        return self._typed_route("paste_list", PasteListRequestResponse, params=raw_params(page=page), normalizer=normalize_pastes)

    def create_paste(self, request: EditPasteRequest) -> RawDataResponse:
        return self._request_route("paste_new", method="POST", data=request)

    def update_paste(self, id: int | str, request: EditPasteRequest) -> RawDataResponse:
        return self._request_route("paste_edit", method="POST", path_params={"id": id}, data=request)

    def delete_paste(self, id: int | str) -> RawDataResponse:
        return self._request_route("paste_delete", method="POST", path_params={"id": id})

    def get_marked_training_list(self, page: int | None = None) -> ProblemSetListRequestResponse:
        return self._typed_route("marked_trainings", ProblemSetListRequestResponse, params=raw_params(page=page), normalizer=normalize_problem_set_list)

    def mark_training(self, id: int | str) -> RawDataResponse:
        return self._request_route("training_mark", method="POST", path_params={"id": id})

    def unmark_training(self, id: int | str) -> RawDataResponse:
        return self._request_route("training_unmark", method="POST", path_params={"id": id})

    def create_training(self, request: EditTrainingRequest) -> RawDataResponse:
        return self._request_route("training_new", method="POST", data=request)

    def update_training(self, id: int | str, request: EditTrainingRequest) -> RawDataResponse:
        return self._request_route("training_edit", method="POST", path_params={"id": id}, data=request)

    def add_training_problem(self, id: int | str, pid: str) -> RawDataResponse:
        return self._request_route("training_add_problem", method="POST", path_params={"id": id}, data={"pid": pid})

    def update_training_problems(self, id: int | str, request: EditTrainingProblemsRequest) -> RawDataResponse:
        return self._request_route("training_edit_problems", method="POST", path_params={"id": id}, data=request)

    def clone_training(self, id: int | str) -> RawDataResponse:
        return self._request_route("training_clone", method="POST", path_params={"id": id})

    def delete_training(self, id: int | str) -> RawDataResponse:
        return self._request_route("training_delete", method="POST", path_params={"id": id})

    def add_problem_to_tasklist(self, pid: str) -> RawDataResponse:
        return self._request_route("problem_tasklist_add", method="POST", data={"pid": pid})

    def remove_problem_from_tasklist(self, pid: str) -> RawDataResponse:
        return self._request_route("problem_tasklist_remove", method="POST", data={"pid": pid})

    def translate_problem(self, pid: str, request: TranslateProblemRequest | None = None) -> RawDataResponse:
        return self._request_route("problem_translate", method="POST", path_params={"pid": pid}, data=request)

    def get_record_list(
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
        return self._typed_route(
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

    def query_downloadable_testcase(self, id: int | str) -> DownloadableTestcaseResponse:
        return self._typed_route("record_downloadable_testcase", DownloadableTestcaseResponse, path_params={"id": id}, normalizer=normalize_downloadable_testcases)

    def download_record_testcase(self, id: int | str, testcase: int | str | None = None) -> bytes:
        return self._send_request(
            endpoint=api_route("record_download_testcase", id=id),
            params=raw_params(testcase=testcase),
            response_type="bytes",
        )

    def get_my_teams(self) -> TeamListRequestResponse:
        return self._typed_route("mine_team", TeamListRequestResponse, normalizer=normalize_teams)

    def join_team(self, id: int | str, request: TeamJoinRequest | None = None) -> RawDataResponse:
        return self._request_route("team_join", method="POST", path_params={"id": id}, data=request)

    def exit_team(self, id: int | str) -> RawDataResponse:
        return self._request_route("team_exit", method="POST", path_params={"id": id})

    def create_team(self, request: EditTeamRequest) -> RawDataResponse:
        return self._request_route("team_create", method="POST", data=request)

    def update_team(self, id: int | str, request: EditTeamRequest) -> RawDataResponse:
        return self._request_route("team_edit", method="POST", path_params={"id": id}, data=request)

    def set_team_master(self, id: int | str, uid: int) -> RawDataResponse:
        return self._request_route("team_set_master", method="POST", path_params={"id": id}, data={"uid": uid})

    def update_team_notice(self, id: int | str, content: str) -> RawDataResponse:
        return self._request_route("team_edit_notice", method="POST", path_params={"id": id}, data={"content": content})

    def update_team_member(self, id: int | str, uid: int, request: TeamMemberUpdateRequest) -> RawDataResponse:
        return self._request_route("team_edit_member", method="POST", path_params={"id": id}, data={"uid": uid, **request})

    def review_team_join_request(self, id: int | str, uid: int, accepted: bool) -> RawDataResponse:
        return self._request_route("team_review", method="POST", path_params={"id": id}, data={"uid": uid, "accepted": accepted})

    def kick_team_member(self, id: int | str, uid: int) -> RawDataResponse:
        return self._request_route("team_kick", method="POST", path_params={"id": id}, data={"uid": uid})

    def get_theme_list(self, page: int | None = None) -> ThemeListRequestResponse:
        return self._typed_route("theme_list", ThemeListRequestResponse, params=raw_params(page=page), normalizer=normalize_themes)

    def get_theme_design(self, id: int | str) -> RawDataResponse:
        return self._request_route("theme_design", path_params={"id": id})

    def set_theme(self, id: int | str) -> RawDataResponse:
        return self._request_route("theme_set", method="POST", path_params={"id": id})

    def create_theme(self, request: EditThemeRequest) -> RawDataResponse:
        return self._request_route("theme_new", method="POST", data=request)

    def update_theme(self, id: int | str, request: EditThemeRequest) -> RawDataResponse:
        return self._request_route("theme_edit", method="POST", path_params={"id": id}, data=request)

    def delete_theme(self, id: int | str) -> RawDataResponse:
        return self._request_route("theme_delete", method="POST", path_params={"id": id})

    def get_user_practice(self, uid: int) -> UserPracticeResponse:
        return self._typed_route("user_practice", UserPracticeResponse, path_params={"uid": uid}, normalizer=normalize_user_practice)

    def get_rating_elo(self, uid: int | None = None) -> RawDataResponse:
        return self._request_route("rating_elo", params=raw_params(uid=uid))

    def get_user_setting(self) -> UserSettingResponse:
        return self._typed_route("user_setting", UserSettingResponse, normalizer=raw_response)

    def get_user_preference(self) -> UserSettingResponse:
        return self._typed_route("user_preference", UserSettingResponse, normalizer=raw_response)

    def update_user_preference(self, request: UserPreferenceUpdateRequest) -> RawDataResponse:
        return self._request_route("user_preference_update", method="POST", data=request)

    def get_user_prize_setting(self) -> UserSettingResponse:
        return self._typed_route("user_prize_setting", UserSettingResponse, normalizer=raw_response)

    def get_user_security_setting(self) -> UserSettingResponse:
        return self._typed_route("user_security_setting", UserSettingResponse, normalizer=raw_response)

    def update_user_slogan(self, slogan: str) -> RawDataResponse:
        return self._request_route("user_update_slogan", method="POST", data={"slogan": slogan})

    def update_user_introduction(self, introduction: str) -> RawDataResponse:
        return self._request_route("user_update_introduction", method="POST", data={"introduction": introduction})

    def update_user_header_image(self, image: str) -> RawDataResponse:
        return self._request_route("user_update_header_image", method="POST", data={"image": image})

    def bind_vjudge_account(self, request: BindRemoteJudgeAccountRequest) -> RawDataResponse:
        return self._request_route("user_bind_vjudge", method="POST", data=request)

    def unbind_vjudge_account(self) -> RawDataResponse:
        return self._request_route("user_unbind_vjudge", method="POST")

    def bind_openid(self, id: int | str, request: OpenIdAuthRequest | None = None) -> RawDataResponse:
        return self._request_route("openid_bind", method="POST", path_params={"id": id}, data=request)

    def unbind_openid(self, id: int | str) -> RawDataResponse:
        return self._request_route("user_unbind_openid", method="POST", path_params={"id": id})
