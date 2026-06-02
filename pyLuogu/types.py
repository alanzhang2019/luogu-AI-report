from __future__ import annotations

from typing import Any, List, Tuple, Literal, Dict, TypeVar, Generic, Mapping, TypeAlias, TypedDict, Union

from .bits.ultility import JsonSerializable, Printable

__all__ = [
    "LuoguType",
    "RequestParams",
    "Response",
    "PagedList",
    "ListRequestParams",
    "ProblemListRequestParams", 
    "ProblemSetListRequestParams",
    "ContestListRequestParams",
    "TeamProblemListRequestParams",
    "UserListRequestParams",
    "RecordListRequestParams", 
    "ThemeListRequestParams",
    "ArticleListRequestParams",
    "BlogListRequestParams",
    "RankingListRequestParams",
    "ProblemRequestParams",
    "UserSearchRequestParams",
    "DiscussionRequestParams",
    "ActivityReuqestParams",
    "ProblemSketch",
    "ProblemSummary",
    "VjudgeSummary",
    "UserSummary",
    "TeamSummary",
    "ProblemContent",
    "Group",
    "TeamMember",
    "Provider",
    "Attachment",
    "ProblemSetSummary",
    "ContestSketch", 
    "Forum",
    "Reply",
    "PostSketch",
    "PostSummary",
    "Prize",
    "EloRatingSummary",
    "ProblemDetails",
    "TestCase",
    "ScoringStrategy",
    "ProblemSettings",
    "TestCaseSettings",
    "UserDetails",
    "ProblemSetDetails",
    "ContestSummary",
    "ContestDetails",
    "ContestSettings",
    "Activity",
    "TeamSettings",
    "TeamDetail",
    "Record",
    "TestCaseStatus",
    "SubtaskStatus",
    "CompileResult",
    "JudgeResult",
    "RecordStatus",
    "RecordDetails",
    "Post",
    "Paste",
    "Image",
    "Article",
    "TagDetail",
    "TagType",
    "ProblemListRequestResponse",
    "ProblemSetListRequestResponse", 
    "ProblemDataRequestResponse",
    "ProblemSettingsRequestResponse",
    "ProblemModifiedResponse",
    "UpdateTestCasesSettingsResponse",
    "ProblemSolutionRequestResponse",
    "ProblemSetDataRequestResponse",
    "ContestDataRequestResponse",
    "ContestListRequestResponse",
    "UserDataRequestResponse",
    "DiscussionRequestResponse",
    "ActivityRequestResponse",
    "TeamDataRequestResponse", 
    "TeamMemberRequestResponse",
    "PasteRequestResponse",
    "ArticleDataRequestResponse",
    "RecordRequestResponse",
    "TagRequestResponse",
    "SubmitCodeResponse",
    "RawDataResponse",
    "EmptyResponse",
    "ArticleListRequestResponse",
    "ArticleReplyListResponse",
    "Blog",
    "BlogListRequestResponse",
    "BlogDataRequestResponse",
    "BlogReplyListResponse",
    "ChatMessage",
    "ChatRecordRequestResponse",
    "ImageListRequestResponse",
    "UploadLink",
    "GenerateUploadLinkResponse",
    "RankingUser",
    "RankingListRequestResponse",
    "Notification",
    "NotificationListRequestResponse",
    "AdvertisementResponse",
    "PaintboardTokenResponse",
    "PasteListRequestResponse",
    "RecordListRequestResponse",
    "DownloadableTestcaseResponse",
    "TeamListRequestResponse",
    "Theme",
    "ThemeListRequestResponse",
    "UserPracticeResponse",
    "UserSettingResponse",
    "AuthResponse",
    "LuoguCookies",
    "ProblemType",
    "ProblemSetType",
    "TransferProblemType",
    "JsonValue",
    "JsonObject",
    "JsonMapping",
    "EditArticleRequest",
    "BatchEditArticleRequest",
    "RegisterRequest",
    "OpenIdAuthRequest",
    "AuthUnlockRequest",
    "EditBlogRequest",
    "BlogAdminForm",
    "ContestJoinRequest",
    "EditContestRequest",
    "EditContestProblemRequest",
    "CreatePostRequest",
    "GenerateUploadLinkRequest",
    "EditPasteRequest",
    "EditTrainingRequest",
    "EditTrainingProblemsRequest",
    "TranslateProblemRequest",
    "TeamJoinRequest",
    "EditTeamRequest",
    "TeamMemberUpdateRequest",
    "EditThemeRequest",
    "UserPreferenceUpdateRequest",
    "BindRemoteJudgeAccountRequest",
]

ProblemType = Literal["P", "U", "T", "B", "CF", "AT", "UVA", "SP"]
ProblemSetType = Literal["official", "select"]
TransferProblemType = Literal["P", "U", "B"] | int
JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = Union[JsonPrimitive, List["JsonValue"], Dict[str, "JsonValue"]]
JsonObject: TypeAlias = Dict[str, JsonValue]
JsonMapping: TypeAlias = Mapping[str, object]


class EditArticleRequest(TypedDict, total=False):
    title: str
    category: int
    content: str
    solutionFor: str | None
    status: int
    top: int


class BatchEditArticleRequest(TypedDict, total=False):
    status: int
    category: int
    lids: List[str]


class RegisterRequest(TypedDict, total=False):
    username: str
    password: str
    endpoint: str
    endpointType: int
    verificationCode: str


class OpenIdAuthRequest(TypedDict, total=False):
    code: str
    state: str
    redirectURI: str


class AuthUnlockRequest(TypedDict, total=False):
    password: str
    captcha: str
    code: str


class EditBlogRequest(TypedDict, total=False):
    title: str
    content: str
    identifier: str
    type: str
    top: int
    status: int
    csrf_token: str


class BlogAdminForm(TypedDict, total=False):
    method: str
    ids: List[int]
    status: int
    type: str


class ContestJoinRequest(TypedDict, total=False):
    code: str
    invitationCode: str
    password: str


class EditContestRequest(TypedDict, total=False):
    settings: JsonObject
    hostID: int | None


class EditContestProblemRequest(TypedDict, total=False):
    problems: List[str]
    pids: List[str]


class CreatePostRequest(TypedDict, total=False):
    captcha: str
    content: str
    title: str
    forum: str


class GenerateUploadLinkRequest(TypedDict, total=False):
    filename: str
    fileName: str
    size: int
    type: str
    mimeType: str


class EditPasteRequest(TypedDict, total=False):
    data: str
    public: bool


class EditTrainingRequest(TypedDict, total=False):
    title: str
    name: str
    description: str
    type: int
    providerID: int | None


class EditTrainingProblemsRequest(TypedDict, total=False):
    problems: List[str]
    pids: List[str]


class TranslateProblemRequest(TypedDict, total=False):
    locale: str
    content: JsonObject


class TeamJoinRequest(TypedDict, total=False):
    reason: str
    message: str


class EditTeamRequest(TypedDict, total=False):
    name: str
    description: str
    notice: str
    contact: JsonObject
    joinPermission: int


class TeamMemberUpdateRequest(TypedDict, total=False):
    realName: str
    group: str
    permission: int


class EditThemeRequest(TypedDict, total=False):
    name: str
    header: JsonObject
    sideNav: JsonObject
    footer: JsonObject


class UserPreferenceUpdateRequest(TypedDict, total=False):
    background: str
    color: str
    theme: int
    language: str


class BindRemoteJudgeAccountRequest(TypedDict, total=False):
    oj: str
    username: str
    password: str
    captcha: str

class LuoguType(JsonSerializable, Printable):
    __type_dict__ = {}

    def __init__(self, json: dict[str, Any] | None = None):
        super().__init__(json)

class RequestParams(LuoguType):
    pass

class Response(LuoguType):
    pass

T_of_list = TypeVar("T_of_list", bound=LuoguType)

class PagedList(LuoguType, Generic[T_of_list]):
    __type_dict__ = {
        "results": [T_of_list],
        "count": int,
        "perPage": int,
    }
    results: List[T_of_list]
    count: int
    perPage: int

class ListRequestParams(RequestParams):
    __type_dict__ = {
        "page": int,
        "orderBy": int
    }

class ProblemListRequestParams(ListRequestParams):
    __type_dict__ = {
        "page": int,
        "orderBy": str,
        "order": str,
        "keyword": str,
        "content": bool,
        "type": str,
        "difficulty": int,
        "tag": str
    }
    page: int
    orderBy: int
    keyword: str
    content: bool
    type: ProblemType
    difficulty: int
    tag: str

class ContestListRequestParams(ListRequestParams):
    __type_dict__ = {
        "page": int,
        "name": str,
        "method": int,
        "public": int
    }

class TeamProblemListRequestParams(ListRequestParams):
    __type_dict__ = {
        "page": int,
        "keyword": str,
        "orderBy": str,
        "order": str
    }

class ProblemSetListRequestParams(ListRequestParams):
    __type_dict__ = {
        "page": int,
        "keyword": str,
        "type": str
    }

class UserListRequestParams(ListRequestParams):
    __type_dict__ = {
        "user": int,
        "page": int,
        "orderBy": int
    }

class RecordListRequestParams(ListRequestParams):
    __type_dict__ = {
        "page": int,
        "pid": str,
        "contestId": int,
        "user": str,
        "status": int,
        "language": int,
        "orderBy": int
    }

class ThemeListRequestParams(ListRequestParams):
    __type_dict__ = {
        "page": int,
        "orderBy": str,
        "order": str,
        "type": str
    }

class ArticleListRequestParams(LuoguType):
    __type_dict__ = {
        "user": int,
        "page": int,
        "category": int,
        "ascending": bool,
        "promoted": bool,
        "title": str
    }

class BlogListRequestParams(ListRequestParams):
    __type_dict__ = {
        "uid": int,
        "keyword": str,
        "type": str,
        "page": int
    }

class RankingListRequestParams(ListRequestParams):
    __type_dict__ = {
        "page": int,
        "orderBy": int
    }

class ProblemRequestParams(RequestParams):
    __type_dict__ = {
        "contestId": int
    }

class UserSearchRequestParams(RequestParams):
    __type_dict__ = {
        "keyword": str
    }

class DiscussionRequestParams(RequestParams):
    __type_dict__ = {
        "page": int,
        "orderBy": int
    }

class ActivityReuqestParams(RequestParams):
    __type_dict__ = {
        "user": int,
        "page": int
    }
    user: int
    page: int

class ProblemSketch(LuoguType):
    __type_dict__ = {
        "pid": str,
        "title": str,
        "difficulty": int,
        "type": str,
        "submitted": bool,
        "accepted": bool
    }
    pid: str
    title: str
    difficulty: int
    type: str
    submitted: bool
    accepted: bool

class ProblemSummary(ProblemSketch):
    __type_dict__ = {
        **ProblemSketch.__type_dict__,
        "tags": [int],
        "totalSubmit": int,
        "totalAccepted": int,
        "flag": int,
        "fullScore": int,
    }
    tags: List[int]
    totalSubmit: int
    totalAccepted: int
    flag: int
    fullScore: int

    def inline(self):
        return f"{self.pid} {self.title} {self.tags} {self.difficulty}"

class VjudgeSummary(LuoguType):
    __type_dict__ = {
        "origin": str,
        "link": str,
        "id": str
    }
    origin: str
    link: str
    id: str

class UserSummary(LuoguType):
    __type_dict__ = {
        "uid": int, 
        "name": str,
        "avatar": str, 
        "slogan": str, 
        "badge": str, 
        "isAdmin": bool, 
        "isBanned": bool, 
        "isRoot": bool, 
        "color": str, 
        "ccfLevel": int, 
        "xcpcLevel": int,
        "background": str, 
    }
    uid: int
    name: str
    avatar: str
    slogan: str
    badge: str
    isAdmin: bool
    isBanned: bool
    color: str
    ccfLevel: int
    xcpcLevel: int
    background: str
    isRoot: bool

class TeamSummary(LuoguType):
    __type_dict__ = {
        "id": int,
        "name": str,
        "isPremium": bool
    }
    id: int
    name: str
    isPremium: bool

class ProblemContent(LuoguType):
    __type_dict__ = {
        "user": UserSummary,
        "version": int,
        "name": str,
        "background": str,
        "description": str,
        "formatI": str,
        "formatO": str,
        "hint": str,
        "locale": str
    }
    user: UserSummary | None
    version: int
    name: str
    background: str
    description: str
    formatI: str
    formatO: str
    hint: str
    locale: str

    def get_markdown(self):
        return "\n## 题目背景\n" + str(self.background) + \
        "\n## 题目描述\n" + str(self.description) + \
        "\n## 输入格式\n" + str(self.formatI) + \
        "\n## 输出格式\n" + str(self.formatO) + \
        "\n## 数据范围与提示\n" + str(self.hint)

class Group(LuoguType):
    __type_dict__ = {
        "id": int,
        "name": str,
        "no": int
    }
    id: int
    name: str
    no: int

class TeamMember(LuoguType):
    __type_dict__ = {
        "group": Group,
        "user": UserSummary,
        "type": int,
        "permission": int,
        "realName": str
    }
    group: Group | None
    user: UserSummary
    type: int
    permission: int
    realName: str

class Provider(LuoguType):
    __type_dict__ = {
        "user": UserSummary,
        "team": TeamSummary
    }
    user: UserSummary | None
    team: TeamSummary | None

    def __init__(self, json: dict[str, Any] | None = None):
        super().__init__(json=None)
        self.user = None
        self.team = None
        if json is None:
            return
        if json.get("uid") is not None:
            self.user = UserSummary(json)
        else:
            self.team = TeamSummary(json)

    def get(self):
        return self.user or self.team

class Attachment(LuoguType):
    __type_dict__ = {
        "size": int,  # 附件大小（字节）
        "uploadTime": int,  # 上传时间（时间戳）
        "downloadLink": str,  # 下载链接
        "id": str,  # 附件 ID
        "filename": str,  # 文件名
        "fileName": str  # legacy alias
    }
    size: int
    uploadTime: int
    downloadLink: str
    id: str
    filename: str | None
    fileName: str | None

    def __init__(self, json: dict[str, Any] | None = None):
        super().__init__(json)
        if self.filename is None and self.fileName is not None:
            self.filename = self.fileName
        if self.fileName is None and self.filename is not None:
            self.fileName = self.filename

class ProblemSetSummary(LuoguType):
    __type_dict__ = {
        "createTime": int,
        "deadline": int,
        "problemCount": int,
        "marked": bool,
        "markCount": int,
        "id": int,
        "name": str,
        "title": str,
        "type": int,
        "provider": Provider
    }
    createTime: int
    deadline: int | None
    problemCount: int
    marked: bool
    markCount: int
    id: int
    name: str | None
    title: str | None
    type: int
    provider: Provider

    def __init__(self, json: dict[str, Any] | None = None):
        super().__init__(json)
        if self.name is None and self.title is not None:
            self.name = self.title
        if self.title is None and self.name is not None:
            self.title = self.name

class ContestSketch(LuoguType):
    __type_dict__ = {
        "id": int,
        "name": str,
        "startTime": int,
        "endTime": int,
    }
    id: int
    name: str
    startTime: int
    endTime: int

class Forum(LuoguType):
    __type_dict__ = {
        "name": str,
        "type": int,
        "slug": str,
        "color": str,
    }
    name: str
    type: int
    slug: str
    color: str

class Reply(LuoguType):
    __type_dict__ = {
        "id": int,
        "content": str,
        "time": int,
        "author": UserSummary,
    }
    id: int
    content: str
    author: UserSummary
    time: int

class PostSketch(LuoguType):
    __type_dict__ = {
        "id": int,
        "title": str,
        "author": UserSummary,
        "time": int
    }
    id: int
    title: str
    author: UserSummary
    time: int

class PostSummary(PostSketch):
    __type_dict__ = {
        "content": str,
        "createTime": int,
        "updateTime": int,
        "forum": Forum,
        "topped": bool,
        "valid": bool,
        "locked": bool,
        "replyCount": int,
    }
    content: str
    createTime: int
    updateTime: int
    forum: Forum
    topped: bool
    valid: bool
    locked: bool
    replyCount: int

class Prize(LuoguType):
    __type_dict__ = {
        "year": int,
        "contestName": str,
        "prize": str
    }
    year: int
    contestName: str
    prize: str

class EloRatingSummary(LuoguType):
    __type_dict__ = {
        "contest": ContestSketch,
        "rating": int,
        "time": int,
        "latest": bool
    }
    contest: ContestSketch
    rating: int
    time: int
    latest: bool

class ProblemDetails(ProblemSummary):
    __type_dict__ = {
        **ProblemSummary.__type_dict__,
        "content": ProblemContent, 
        # "contenu" : ???,
        "samples": [(str, str)],
        "provider": Provider,
        "attachments": [Attachment],
        "limits": [(int, int)],
        "showScore": bool,
        "score": int,
        "stdCode": str,
        "vjudge": VjudgeSummary,
        "acceptLanguages": [int]
    }
    content: ProblemContent
    samples: List[Tuple[str, str]]
    provider: Provider
    attachments: List[Attachment]
    limits: List[Tuple[int, int]]
    showScore: bool
    score: int
    stdCode: str
    vjudge: VjudgeSummary | None
    acceptLanguages: List[int]

class TestCase(LuoguType):
    __type_dict__ = {
        "upid": int,  # 测试用例唯一 ID
        "inputFileName": str,  # 输入文件名
        "outputFileName": str,  # 输出文件名
        "timeLimit": int,  # 时间限制（毫秒）
        "memoryLimit": int,  # 内存限制（MB）
        "fullScore": int,  # 满分
        "isPretest": bool,  # 是否为预测试
        "subtaskId": int  # 所属子任务 ID
    }
    upid: int
    inputFileName: str
    outputFileName: str
    timeLimit: int
    memoryLimit: int
    fullScore: int
    isPretest: bool
    subtaskId: int

class ScoringStrategy(LuoguType):
    __type_dict__ = {
        "type": int,    # 评分策略类型
        "script": str   # 评分脚本内容
    }
    type: int
    script: str | None

class ProblemSettings(LuoguType):
    __type_dict__ = {
        "title": str,
        "background": str,
        "description": str,
        "inputFormat": str,
        "outputFormat": str,
        "samples": [(str, str)],
        "hint": str,
        "translation": str,
        "comment": str,
        "needsTranslation": bool,
        "acceptSolution": bool,
        "allowDataDownload": bool,
        "tags": [int],
        "difficulty": int,
        "showScore": bool,
        "providerID": int,
        "flag": int
    }
    title: str
    background: str
    description: str
    inputFormat: str
    outputFormat: str
    samples: List[Tuple[str, str]]
    hint: str
    comment: str
    translation: str
    needsTranslation: bool
    acceptSolution: bool
    allowDataDownload: bool
    tags: List[int]
    difficulty: int
    showScore: bool
    providerID: int
    flag: int

    @staticmethod
    def get_default():
        return ProblemSettings(
            json={
                "title": "",
                "background": "",
                "description": "",
                "inputFormat": "",
                "outputFormat": "",
                "samples": [],
                "hint": "",
                "comment": "",
                "translation": "",
                "needsTranslation": False,
                "acceptSolution": True,
                "allowDataDownload": False,
                "tags": [],
                "difficulty": 0,
                "showScore": True,
                "providerID": None,
                "flag": 0
            }
        )
    
    def get_markdown(self):
        return "\n## 题目背景\n" + str(self.background) + \
        "\n## 题目描述\n" + str(self.description) + \
        "\n## 输入格式\n" + str(self.inputFormat) + \
        "\n## 输出格式\n" + str(self.outputFormat) + \
        "\n## 数据范围与提示\n" + str(self.hint)
    
    def append_tags(self, tags: List[int] | int ):
        if isinstance(tags, int):
            tags = [tags]
        for tag in tags:
            if tag not in self.tags:
                self.tags.append(tag)

    def remove_tags(self, tags: List[int] | int):
        if isinstance(tags, int):
            tags = [tags]
        for tag in tags:
            if tag in self.tags:
                self.tags.remove(tag)

class TestCaseSettings(LuoguType):
    __type_dict__ = {
        "cases": [TestCase],  # 测试用例列表
        "subtaskScoringStrategies": {str: ScoringStrategy},  # 子任务评分策略（字典）
        "scoringStrategy": ScoringStrategy,  # 总评分策略
        "showSubtask": bool  # 是否显示子任务
    } 
    cases: List[TestCase] 
    subtaskScoringStrategies: Dict[str, ScoringStrategy]
    scoringStrategy: ScoringStrategy
    showSubtask: bool

class UserDetails(UserSummary):
    __type_dict__ = {
        **UserSummary.__type_dict__,
        "followingCount": int,
        "followerCount": int,
        "ranking": int,
        # "rating": 'Rating', # aka guzhi
        "registerTime": int,
        "introduction": str,
        "prize": [Prize],
        "elo": EloRatingSummary,
        "eloMax": EloRatingSummary,
        "userRelationship": int,
        "reverseUserRelationship": int,
        "passedProblemCount": int,
        "submittedProblemCount": int
    }
    followingCount: int
    followerCount: int
    ranking: int
    eloValue: int
    # rating: Rating
    registerTime: int
    introduction: str
    prize: List[Prize]
    elo: EloRatingSummary
    eloMax: EloRatingSummary
    userRelationship: int
    reverseUserRelationship: int
    passedProblemCount: int
    submittedProblemCount: int

class ProblemSetDetails(ProblemSetSummary):
    __type_dict__ = {
        **ProblemSetSummary.__type_dict__,
        "description": str,
        "problems": [ProblemSummary],
        # "userScore": Optional[Dict[str, Union[UserSummary, int, Dict[str, Optional[int]], Dict[str, bool]]]],
    }
    description: str
    problems: List[ProblemSummary]
    # userScore: Optional[Dict[str, Union[UserSummary, int, Dict[str, Optional[int]], Dict[str, bool]]]]
    
class ContestSummary(ContestSketch):
    __type_dict__ = {
        **ContestSketch.__type_dict__,
        "method": int,
        "visibility": int,
        "ruleType": int,
        "visibilityType": int,
        "invitationCodeType": int,
        "rated": int,
        "problemCount": int,
        "host": Provider,
        "squad": bool,
    }
    method: int | None
    visibility: int | None
    ruleType: int | None
    visibilityType: int | None
    invitationCodeType: int | None
    rated: bool | int | None
    problemCount: int | None
    host: Provider | None
    squad: bool | None

    def __init__(self, json: dict[str, Any] | None = None):
        super().__init__(json)
        if self.method is None and self.ruleType is not None:
            self.method = self.ruleType
        if self.ruleType is None and self.method is not None:
            self.ruleType = self.method
        if self.visibility is None and self.visibilityType is not None:
            self.visibility = self.visibilityType
        if self.visibilityType is None and self.visibility is not None:
            self.visibilityType = self.visibility

class ContestDetails(ContestSummary):
    __type_dict__ = {
        **ContestSummary.__type_dict__,
        "description": str,
        "totalParticipants": int,
        "eloThreshold": int,
        "eloDone": bool,
        "canEdit": bool,
        "problems": [ProblemSummary],
        "isScoreboardFrozen": bool,
    }
    description: str
    totalParticipants: int
    eloDone: bool
    eloThreshold: int
    canEdit: bool
    problems: List[ProblemSummary]
    isScoreboardFrozen: bool

class ContestSettings(LuoguType):
    __type_dict__ = {
        "name": str,
        "description": str,
        "visibilityType": int,
        "invitationCodeType": int,
        "ruleType": int,
        "startTime": int,
        "endTime": int,
        "rated": bool,
        "ratingGroup": str,
        "eloThreshold": int,
        "eloCenter": int,
    }
    name: str
    description: str
    visibilityType: int
    invitationCodeType: int
    ruleType: int
    startTime: int
    endTime: int
    rated: bool
    ratingGroup: str | None
    eloThreshold: int | None
    eloCenter: int | None

class Activity(LuoguType):
    __type_dict__ = {
        "content": str,
        "id": int,
        "type": int,
        "time": int,
        "user": UserSummary
    }
    content: str
    id: int
    type: int
    time: int
    user: UserSummary

class TeamSettings(LuoguType):
    __type_dict__ = {
        "description": str,
        "notice": str,
        "contact": {str: str},
        "joinPermission": int
    }
    description: str
    notice: str
    contact: Dict[str, str]
    joinPermission: int

class TeamDetail(TeamSummary):
    __type_dict__ = {
        **TeamSummary.__type_dict__,
        "createTime": int,
        "master": UserSummary,
        "setting": TeamSettings,
        "premiumUntil": int,
        "type": int,
        "memberCount": int
    }
    createTime: int
    master: UserSummary
    setting: TeamSettings
    premiumUntil: int | None
    type: int
    memberCount: int

class Record(LuoguType):
    __type_dict__ = {
        "time": int,
        "memory": int,
        "problem": ProblemSketch,
        "contest": ContestSummary,
        "sourceCodeLength": int,
        "submitTime": int,
        "language": int,
        "user": UserSummary,
        "id": int,
        "status": int,
        "enableO2": bool,
        "score": int
    }
    time: int | None
    memory: int | None
    problem: ProblemSketch
    contest: ContestSummary | None
    sourceCodeLength: int
    submitTime: int
    language: int
    user: UserSummary | None
    id: int
    status: int
    enableO2: bool
    score: int | None

class TestCaseStatus(LuoguType):
    __type_dict__ = {
        "id": int,
        "status": int,
        "time": int,
        "memory": int,
        "score": int,
        "signal": int,
        "exitCode": int,
        "description": str,
        "subtaskID": int
    }
    id: int
    status: int
    time: int
    memory: int
    score: int
    signal: int | None
    exitCode: int
    description: str | int
    subtaskID: int

class SubtaskStatus(LuoguType):
    __type_dict__ = {
        "id": int,
        "score": int,
        "status": int,
        "testCases": [TestCaseStatus],
        "judger": str,
        "time": int,
        "memory": int
    }
    id: int
    score: int
    status: int
    testCases: List[TestCaseStatus]
    judger: str | None
    time: int
    memory: int

class CompileResult(LuoguType):
    __type_dict__ = {
        "success": bool,
        "message": str,
        "opt2": bool
    }
    success: bool
    message: str | None
    opt2: bool

class JudgeResult(LuoguType):
    __type_dict__ = {
        # "subtasks": [SubtaskStatus],
        "finishedCaseCount": int,
        "status": int,
        "time": int,
        "memory": int,
        "score": int
    }
    # subtasks: List[SubtaskStatus]
    finishedCaseCount: int
    status: int
    time: int
    memory: int
    score: int

class RecordStatus(LuoguType):
    __type_dict__ = {
        "compileResult": CompileResult,
        "judgeResult": JudgeResult,
        "version": int
    }
    compileResult: CompileResult | None
    judgeResult: JudgeResult | None
    version: int

class RecordDetails(Record):
    __type_dict__ = {
        **Record.__type_dict__,
        "detail": RecordStatus,
        "sourceCode": str
    }
    detail: "RecordStatus"
    sourceCode: str | None

class Post(PostSummary):
    __type_dict__ = {
        **PostSummary.__type_dict__,
        "pinnedReply": Reply,
        "content": str
    }
    pinnedReply: Reply | None
    content: str

class Paste(LuoguType):
    __type_dict__ = {
        "data": str,
        "id": str,
        "user": UserSummary,
        "time": int,
        "public": bool
    }
    data: str
    id: str
    user: UserSummary
    time: int
    public: bool

class Image(LuoguType):
    __type_dict__ = {
        "thumbnailUrl": str,
        "url": str,
        "id": str,
        "provider": UserSummary,
        "uploadTime": int,
        "size": int
    }
    thumbnailUrl: str
    url: str
    id: str
    provider: UserSummary
    uploadTime: int
    size: int

class Article(LuoguType):
    __type_dict__ = {
        "lid": str,
        "title": str,
        "time": int,
        "author": UserSummary,
        "upvote": int,
        "replyCount": int,
        "favorCount": int,
        "category": int,
        "status": int,
        "solutionFor": ProblemSketch,
        "promoteStatus": int,
        # "collection": Optional[Any],
        "content": str,
        "categoryOld": str,
        "contentFull": bool,
        "adminNote": str,
        "adminComment": str,
        "voted": int,
        "canReply": bool,
        "canEdit": bool
    }
    lid: str
    title: str
    time: int
    author: UserSummary
    upvote: int
    replyCount: int
    favorCount: int
    category: int
    status: int
    solutionFor: ProblemSketch
    promoteStatus: int
    # collection: Optional[Any]
    content: str
    categoryOld: str
    contentFull: bool
    adminNote: str | None
    adminComment: str
    voted: int | None
    canReply: bool
    canEdit: bool

class TagDetail(LuoguType):
    __type_dict__ = {
        "id": int,
        "name": str,
        "type": int,
        "parent": int
    }
    id: int
    name: str
    type: int
    parent: int | None

class TagType(LuoguType):
    __type_dict__ = {
        "id": int,
        "name": str,
        "color": str
    }
    id: int
    name: str
    color: str

class ProblemListRequestResponse(Response):
    __type_dict__ = {
        "problems": [ProblemSummary],
        "count": int,
        "perPage": int,
    }
    problems : List[ProblemSummary]
    count : int
    perPage: int

class ProblemSetListRequestResponse(Response):
    __type_dict__ = {
        "trainings": [ProblemSetSummary],
        "count": int,
        "perPage": int,
        "page": int
    }
    problems : List[ProblemSetSummary]
    count : int
    perPage: int
    page: int

class ProblemDataRequestResponse(LuoguType):
    __type_dict__ = {
        "problem": ProblemDetails,
        "translations": {str: ProblemContent},
        "bookmarked": bool,
        "contest": ContestSketch,
        "vjudgeUsername": str,
        "lastLanguage": int,
        "lastCode": str,
        "recommendations": [ProblemSketch],
        "forum": Forum,
        "discussions": [PostSketch],
        "canEdit": bool
    }
    problem: ProblemDetails
    translations: Dict[str, ProblemContent]
    bookmarked: bool
    contest: ContestSketch | None
    vjudgeUsername: str | None
    lastLanguage: int
    lastCode: str
    recommendations: List[ProblemSummary]
    discussions: List[PostSummary]
    canEdit: bool

class ProblemSettingsRequestResponse(Response):
    __type_dict__ = {
        # "problemDetails": LegacyProblemDetails,
        "problemSettings": ProblemSettings,
        "testCaseSettings": TestCaseSettings,
        # "clonedFrom": dict,
        "isClonedTestCases": bool,
        "updating": bool,
        "testDataDownloadLink": str,
        # "updateStatus": {
        #     "success": bool,
        #     "message": str
        # }
        "isProblemAdmin": bool,
        "privilegedTeams": [TeamSummary]
    }
    problemDetails: ProblemDetails
    problemSettings: ProblemSettings
    testCaseSettings: TestCaseSettings

class ProblemModifiedResponse(Response):
    __type_dict__ = {
        "pid": str
    }
    pid: str

class UpdateTestCasesSettingsResponse(Response):
    __type_dict__ = {
        "problem": ProblemDetails,
        "testCases": [TestCase],
        "scoringStrategy": ScoringStrategy,
        "subtaskScoringStrategies": {str: ScoringStrategy}
    }
    problem: ProblemDetails
    testCases: List[TestCase]
    scoringStrategy: ScoringStrategy
    subtaskScoringStrategies: Dict[str, ScoringStrategy]

class ProblemSolutionRequestResponse(Response):
    __type_dict__ = {
        "perPage": int,
        "count": int,
        "solutions": [Article],
        "problem": ProblemSketch,
        "acceptSolution": bool,
    }
    perPage: int
    count: int
    solutions: List[Article]
    problem: ProblemSketch
    acceptSolution: bool

class ProblemSetDataRequestResponse(Response):
    __type_dict__ = {
        "training": ProblemSetDetails,
        # "trainingProblems": {
        #    "result": List[List[Any]],
        #    "perPage": type(None),
        #    "count": int
        # },
        "canEdit": bool,
        "privilegedTeams": [TeamSummary]
    }
    training: ProblemSetDetails
    # trainingProblems: Dict[str, Union[List[List[Any]], None, int]]
    canEdit: bool
    privilegedTeams: List[TeamSummary]

class ContestDataRequestResponse(Response):
    __type_dict__ = {
        "contest": ContestDetails,
        "joined": bool,
        "accessLevel": int,
        # "userElo": EloRatingSummary
        # "userScore" : Optional[Dict[str, Union[UserSummary, int, Dict[str, Optional[int]], Dict[str, bool]]]]
    }
    contest : ContestDetails
    joined : bool
    accessLevel : int

class ContestListRequestResponse(Response):
    __type_dict__ = {
        "contests": [ContestSummary],
        "count": int,
        "perPage": int
    }
    contests: List[ContestSummary]
    count: int
    perPage: int

class UserDataRequestResponse(LuoguType):
    __type_dict__ = {
        "user": UserDetails,
        "passedProblems": [ProblemSummary],
        "submittedProblems": [ProblemSummary],
        "teams": [TeamSummary]
    }
    user: UserDetails
    passedProblems: List['ProblemSummary']
    submittedProblems: List['ProblemSummary']
    teams: List[TeamSummary]

class DiscussionRequestResponse(Response):
    __type_dict__ = {
        "forum": Forum,
        "post": Post,
        "count": int,
        "perPage": int,
        "replies": [Reply],
        "canReply": bool,
    }
    forum: Forum
    post: Post
    count: int
    perPage: int 
    replies: List[Reply]
    canReply: bool

class ActivityRequestResponse(Response):
    __type_dict__ = {
        "activities": [Activity],
        "count": int,
        "perPage": int,
    }
    activities: List[Activity]
    count: int
    perPage: int

class TeamDataRequestResponse(Response):
    __type_dict__ = {
        "team": TeamDetail,
        "currentTeamMember": TeamMember,
        "latestDiscussions": [PostSummary],
        "groups": [Group],
        "usages": {str: (int, int)}
    }
    team: TeamDetail
    currentTeamMember: TeamMember | None
    latestDiscussions: PostSummary | None
    groups: List[Group]
    usages: Dict[str, Tuple[int, int]]

class TeamMemberRequestResponse(Response):
    __type_dict__ = {
        "members": [TeamMember],
        "perPage": int,
        "count": int,
        "group": Group,
        # "groupMemberCount": {int: int}
    }
    member: TeamMember
    perPage: int
    count: int
    group: Group
    # groupMemberCount: Dict[int, int]

class PasteRequestResponse(Response):
    __type_dict__ = {
        "paste": Paste,
        "canEdit": bool
    }
    paste: Paste
    canEdit: bool

class ArticleDataRequestResponse(Response):
    __type_dict__ = {
        "article": Article,
        "favored": bool,
        "voted": int,
        "canReply": bool,
        "canEdit": bool
    }
    article: Article
    favored: bool
    voted: int | None
    canReply: bool
    canEdit: bool

class RecordRequestResponse(Response):
    __type_dict__ = {
        "record": RecordDetails,
        # "testCaseGroup": List[List[int]] | Dict[int, List[int]],
        "showStatus": bool
    }
    record: RecordDetails
    # testCaseGroup: List[List[int]] | Dict[int, List[int]]
    showStatus: bool

class TagRequestResponse(Response):
    __type_dict__ = {
        "tags": [TagDetail],
        "types": [TagType]
    }
    tags: List[TagDetail]
    types: List[TagType]

class SubmitCodeResponse(Response):
    __type_dict__ = {
        "rid" : int
    }
    rid: int

class RawDataResponse(Response):
    __type_dict__ = {
        "data": Any
    }
    data: Any

class EmptyResponse(Response):
    __type_dict__ = {
        "ok": bool,
        "_empty": bool,
    }
    ok: bool
    _empty: bool

class ArticleListRequestResponse(Response):
    __type_dict__ = {
        "articles": [Article],
        "count": int,
        "perPage": int,
    }
    articles: List[Article]
    count: int
    perPage: int

class ArticleReplyListResponse(Response):
    __type_dict__ = {
        "replies": [Reply],
        "count": int,
        "perPage": int,
    }
    replies: List[Reply]
    count: int
    perPage: int

class Blog(LuoguType):
    __type_dict__ = {
        "id": int,
        "title": str,
        "content": str,
        "identifier": str,
        "type": str,
        "status": int,
        "top": int,
        "time": int,
        "author": UserSummary,
        "replyCount": int,
        "upvote": int,
    }
    id: int
    title: str
    content: str | None
    identifier: str | None
    type: str | None
    status: int | None
    top: int | None
    time: int | None
    author: UserSummary | None
    replyCount: int | None
    upvote: int | None

class BlogListRequestResponse(Response):
    __type_dict__ = {
        "blogs": [Blog],
        "count": int,
        "perPage": int,
    }
    blogs: List[Blog]
    count: int
    perPage: int

class BlogDataRequestResponse(Response):
    __type_dict__ = {
        "blog": Blog,
        "canEdit": bool,
        "canReply": bool,
        "voted": int,
    }
    blog: Blog
    canEdit: bool | None
    canReply: bool | None
    voted: int | None

class BlogReplyListResponse(Response):
    __type_dict__ = {
        "replies": [Reply],
        "count": int,
        "perPage": int,
    }
    replies: List[Reply]
    count: int
    perPage: int

class ChatMessage(LuoguType):
    __type_dict__ = {
        "id": int,
        "sender": UserSummary,
        "receiver": UserSummary,
        "content": str,
        "time": int,
        "read": bool,
    }
    id: int
    sender: UserSummary | None
    receiver: UserSummary | None
    content: str
    time: int
    read: bool | None

class ChatRecordRequestResponse(Response):
    __type_dict__ = {
        "records": [ChatMessage],
        "count": int,
        "perPage": int,
    }
    records: List[ChatMessage]
    count: int
    perPage: int

class ImageListRequestResponse(Response):
    __type_dict__ = {
        "images": [Image],
        "count": int,
        "perPage": int,
    }
    images: List[Image]
    count: int
    perPage: int

class UploadLink(LuoguType):
    __type_dict__ = {
        "host": str,
        "policy": str,
        "accessKeyID": str,
        "callback": str,
        "signature": str,
        "expiredTime": int,
        "dir": str,
    }
    host: str
    policy: str
    accessKeyID: str
    callback: str
    signature: str
    expiredTime: int
    dir: str

class GenerateUploadLinkResponse(Response):
    __type_dict__ = {
        "uploadLink": UploadLink
    }
    uploadLink: UploadLink

class RankingUser(UserSummary):
    __type_dict__ = {
        **UserSummary.__type_dict__,
        "ranking": int,
        "rating": int,
        "elo": EloRatingSummary,
    }
    ranking: int | None
    rating: int | None
    elo: EloRatingSummary | None

class RankingListRequestResponse(Response):
    __type_dict__ = {
        "users": [RankingUser],
        "count": int,
        "perPage": int,
    }
    users: List[RankingUser]
    count: int
    perPage: int

class Notification(LuoguType):
    __type_dict__ = {
        "id": int,
        "type": int,
        "content": str,
        "time": int,
        "read": bool,
        "sender": UserSummary,
    }
    id: int
    type: int
    content: str
    time: int
    read: bool | None
    sender: UserSummary | None

class NotificationListRequestResponse(Response):
    __type_dict__ = {
        "notifications": [Notification],
        "count": int,
        "perPage": int,
    }
    notifications: List[Notification]
    count: int
    perPage: int

class AdvertisementResponse(Response):
    __type_dict__ = {
        "data": Any
    }
    data: Any

class PaintboardTokenResponse(Response):
    __type_dict__ = {
        "token": str
    }
    token: str

class PasteListRequestResponse(Response):
    __type_dict__ = {
        "pastes": [Paste],
        "count": int,
        "perPage": int,
    }
    pastes: List[Paste]
    count: int
    perPage: int

class RecordListRequestResponse(Response):
    __type_dict__ = {
        "records": [Record],
        "count": int,
        "perPage": int,
    }
    records: List[Record]
    count: int
    perPage: int

class DownloadableTestcaseResponse(Response):
    __type_dict__ = {
        "testcases": [str],
        "data": Any
    }
    testcases: List[str]
    data: Any

class TeamListRequestResponse(Response):
    __type_dict__ = {
        "teams": [TeamSummary],
        "count": int,
        "perPage": int,
    }
    teams: List[TeamSummary]
    count: int
    perPage: int

class Theme(LuoguType):
    __type_dict__ = {
        "id": int,
        "name": str,
        "user": UserSummary,
        "header": {str: Any},
        "sideNav": {str: Any},
        "footer": {str: Any},
        "createTime": int,
        "updateTime": int,
    }
    id: int
    name: str
    user: UserSummary | None
    header: Dict[str, Any] | None
    sideNav: Dict[str, Any] | None
    footer: Dict[str, Any] | None
    createTime: int | None
    updateTime: int | None

class ThemeListRequestResponse(Response):
    __type_dict__ = {
        "themes": [Theme],
        "count": int,
        "perPage": int,
    }
    themes: List[Theme]
    count: int
    perPage: int

class UserPracticeResponse(Response):
    __type_dict__ = {
        "problems": [ProblemSummary],
        "count": int,
        "perPage": int,
        "data": Any,
    }
    problems: List[ProblemSummary]
    count: int
    perPage: int
    data: Any

class UserSettingResponse(Response):
    __type_dict__ = {
        "user": UserDetails,
        "settings": {str: Any},
        "data": Any,
    }
    user: UserDetails | None
    settings: Dict[str, Any] | None
    data: Any

class AuthResponse(Response):
    __type_dict__ = {
        "user": UserDetails,
        "locked": bool,
        "data": Any,
    }
    user: UserDetails | None
    locked: bool | None
    data: Any

class LuoguCookies(LuoguType):
    __type_dict__ = {
        "__client_id": str,
        "_uid": str,
    }
    __client_id: str
    _uid: str

    def __init__(self, json: dict[str, Any] | None = None):
        self._raw: dict[str, Any] = {}
        if isinstance(json, dict):
            self._raw = dict(json)
        super().__init__(json=json)

    def to_json(self):
        base = super().to_json()
        if not self._raw:
            return base
        merged = dict(self._raw)
        merged.update(base)
        return merged
