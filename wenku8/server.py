"""FastAPI 服务：把 Wenku8API 的只读爬取方法暴露为 HTTP 端点。

单例模型——库持有常驻 HTTP 客户端，只有登录和 CF 质询兜底会启动浏览器；整个服务共享一个
`Wenku8API` 实例。启动时用 `.env` 的 `WENKU8_USERNAME` / `WENKU8_PASSWORD`
自动登录，所有请求复用该登录态（无 `/login` 端点）。

并发：普通 HTML、CDN/二进制和缓存路径均可并发；只有 Chromium 兜底导航串行。
HTTP 429 会遵循 Retry-After（最多 5 秒）重试一次，不对正常搜索施加固定等待。

运行：`env DISPLAY=:0 WENKU8_HEADLESS=0 .venv/bin/python -m wenku8.server`
监听：`WENKU8_HOST`（默认 127.0.0.1）/ `WENKU8_PORT`（默认 8000）
文档：`/docs`
"""
import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Path, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BeforeValidator

from wenku8.api import Wenku8API
from wenku8.consts import Lang, NovelSortMethod, SearchMethod
from wenku8.exceptions import (
    CloudflareChallengeException,
    InvalidUrlError,
    NotLoggedInException,
    PageParseError,
    RateLimitException,
)
from wenku8.models import (
    BookshelfItem,
    CommentItem,
    NovelCover,
    NovelIndex,
    NovelInfo,
    RecommendBlock,
    ReplyItem,
    SearchResult,
    UserInfo,
)

logger = logging.getLogger("wenku8.server")


def _env_size_mb(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default * 1024 * 1024
    return max(0, int(value)) * 1024 * 1024


def get_api(request: Request) -> Wenku8API:
    """从 app.state 取单例 Wenku8API。"""
    return request.app.state.api


ApiDep = Annotated[Wenku8API, Depends(get_api)]


def _coerce_lang(v):
    """lang 查询参数优先按名字（zh_CN/zh_TW）匹配，兼容值（gbk/big5）。

    Lang 是 StrEnum，值是 gbk/big5（供库内部 charset 用），但 API 调用方自然
    传入的是名字 zh_CN/zh_TW，故在入参处转换。
    """
    if isinstance(v, Lang):
        return v
    if isinstance(v, str):
        s = v.strip()
        if s in Lang.__members__:
            return Lang[s]
        try:
            return Lang(s)
        except ValueError:
            pass
    raise ValueError("lang 必须是 zh_CN 或 zh_TW")


LangQ = Annotated[  # noqa: N816
    Lang,
    BeforeValidator(_coerce_lang),
    Query(description="输出语言：zh_CN / zh_TW", examples=["zh_CN"],
          json_schema_extra={"enum": ["zh_CN", "zh_TW"]}),
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动自动登录（.env 凭据），关闭释放浏览器。"""
    load_dotenv()
    api = Wenku8API(
        endpoint=os.environ.get("WENKU8_ENDPOINT", "https://www.wenku8.cc"),
        cache_memory_bytes=_env_size_mb("WENKU8_CACHE_MEMORY_MB", 64),
    )  # headless 由 WENKU8_HEADLESS 驱动
    username = os.environ.get("WENKU8_USERNAME")
    password = os.environ.get("WENKU8_PASSWORD")
    if username and password:
        try:
            await api.login(username, password)
            print(f"[wenku8] 启动登录成功，logged_in={api.is_logged_in}", flush=True)
        except Exception as e:  # 登录失败不阻断启动，受保护端点将返回 401
            print(f"[wenku8] 启动登录失败：{e!r}；受保护端点将返回 401", flush=True)
    else:
        print("[wenku8] .env 未设置 WENKU8_USERNAME/WENKU8_PASSWORD，跳过自动登录", flush=True)
    app.state.api = api
    try:
        yield
    finally:
        await api.close()


app = FastAPI(title="pywenku8api", version="0.1.0", lifespan=lifespan)


def _error(status: int, exc: Exception, type_name: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": str(exc), "type": type_name})


@app.exception_handler(NotLoggedInException)
async def _handle_not_logged_in(_: Request, exc: NotLoggedInException):
    return _error(401, exc, "not_logged_in")


@app.exception_handler(RateLimitException)
async def _handle_rate_limit(_: Request, exc: RateLimitException):
    return _error(429, exc, "rate_limited")


@app.exception_handler(CloudflareChallengeException)
async def _handle_cf_challenge(_: Request, exc: CloudflareChallengeException):
    return _error(502, exc, "cloudflare_challenge")


@app.exception_handler(PageParseError)
async def _handle_parse_error(_: Request, exc: PageParseError):
    return _error(502, exc, "page_parse_error")


@app.exception_handler(InvalidUrlError)
async def _handle_invalid_url(_: Request, exc: InvalidUrlError):
    return _error(400, exc, "invalid_url")


@app.exception_handler(Exception)
async def _handle_unexpected(_: Request, exc: Exception):
    """兜底：CF 质询超时(TimeoutError)、CDN HTTPStatusError、解析 IndexError 等
    未识别异常归一为 502；完整堆栈写日志便于本地排查，响应只暴露 type 名。"""
    logger.error("未处理的异常，归一为 502: %r", exc, exc_info=exc)
    return _error(502, exc, type(exc).__name__)


# ----- 状态 -----

@app.get("/health", tags=["meta"])
async def health(api: ApiDep):
    """服务健康与登录态自检。"""
    return {"logged_in": api.is_logged_in}


@app.get("/cache/status", tags=["meta"])
async def cache_status(api: ApiDep):
    """缓存命中率、占用空间和正在合并的上游请求数。"""
    return await api.cache_stats()


# ----- 小说 -----

@app.get("/novel/cover/{aid}", tags=["novel"], response_class=Response)
async def novel_cover(aid: Annotated[int, Path(description="文章 ID")], api: ApiDep):
    """书籍封面图（CDN，JPEG）。"""
    data = await api.get_novel_cover(aid)
    return Response(content=data, media_type="image/jpeg")


@app.get("/novel/info/{aid}", tags=["novel"], response_model=NovelInfo)
async def novel_info(aid: Annotated[int, Path(description="文章 ID")], api: ApiDep, lang: LangQ = Lang.zh_CN):
    return await api.get_novel_info(aid, lang=lang)


@app.get("/novel/index/{aid}", tags=["novel"], response_model=NovelIndex)
async def novel_index(aid: Annotated[int, Path(description="文章 ID")], api: ApiDep, lang: LangQ = Lang.zh_CN):
    return await api.get_novel_index(aid, lang=lang)


@app.get("/novel/content/{aid}/{cid}", tags=["novel"], response_class=PlainTextResponse)
async def novel_content(
    aid: Annotated[int, Path(description="文章 ID")],
    cid: Annotated[int, Path(description="章节 ID")],
    api: ApiDep,
    lang: LangQ = Lang.zh_CN,
):
    return await api.get_novel_content(aid, cid, lang=lang)


@app.get("/novel/full/{aid}", tags=["novel"], response_class=PlainTextResponse)
async def novel_full(aid: Annotated[int, Path(description="文章 ID")], api: ApiDep, lang: LangQ = Lang.zh_CN):
    """整本小说（UTF-8 TXT，CDN，有界内存缓存）。"""
    return await api.get_full_novel_content(aid, lang=lang)


@app.get("/novel/content_via_full/{aid}/{cid}", tags=["novel"], response_class=PlainTextResponse)
async def novel_content_via_full(
    aid: Annotated[int, Path(description="文章 ID")],
    cid: Annotated[int, Path(description="章节 ID")],
    api: ApiDep,
    lang: LangQ = Lang.zh_CN,
):
    return await api.get_novel_content_via_full(aid, cid, lang=lang)


# ----- 搜索与列表 -----

@app.get("/search", tags=["search"], response_model=SearchResult)
async def search(
    keyword: Annotated[str, Query(description="搜索关键词")],
    method: SearchMethod,
    api: ApiDep,
    page: Annotated[int, Query(ge=1)] = 1,
    lang: LangQ = Lang.zh_CN,
):
    return await api.search_novel(keyword, method, page, lang)


@app.get("/search/by_name", tags=["search"], response_model=SearchResult)
async def search_by_name(
    keyword: Annotated[str, Query(description="搜索关键词")],
    api: ApiDep,
    page: Annotated[int, Query(ge=1)] = 1,
    lang: LangQ = Lang.zh_CN,
):
    return await api.search_novel_by_name(keyword, page, lang)


@app.get("/search/by_author", tags=["search"], response_model=SearchResult)
async def search_by_author(
    keyword: Annotated[str, Query(description="搜索关键词")],
    api: ApiDep,
    page: Annotated[int, Query(ge=1)] = 1,
    lang: LangQ = Lang.zh_CN,
):
    return await api.search_novel_by_author(keyword, page, lang)


@app.get("/novel/list", tags=["search"], response_model=SearchResult)
async def novel_list(
    sort: NovelSortMethod,
    api: ApiDep,
    page: Annotated[int, Query(ge=1)] = 1,
    lang: LangQ = Lang.zh_CN,
):
    return await api.get_novel_list(sort, page, lang)


@app.get("/category", tags=["search"], response_model=SearchResult)
async def category(
    tag: Annotated[str, Query(description="分类标签")],
    sort: NovelSortMethod,
    api: ApiDep,
    page: Annotated[int, Query(ge=1)] = 1,
    lang: LangQ = Lang.zh_CN,
):
    return await api.get_novel_by_category(tag, sort, page, lang)


@app.get("/finished", tags=["search"], response_model=SearchResult)
async def finished(api: ApiDep, page: Annotated[int, Query(ge=1)] = 1, lang: LangQ = Lang.zh_CN):
    return await api.get_finished_novels(page, lang)


# ----- 书架 / 用户 / 评论 / 推荐 -----

@app.get("/bookshelf", tags=["user"], response_model=list[BookshelfItem])
async def bookshelf(api: ApiDep, bid: Annotated[int, Query(ge=0)] = 0, lang: LangQ = Lang.zh_CN):
    return await api.get_bookshelf(bid, lang)


@app.get("/user/bookshelf/{uid}", tags=["user"], response_model=list[NovelCover])
async def user_bookshelf(uid: Annotated[int, Path(description="用户 UID")], api: ApiDep, lang: LangQ = Lang.zh_CN):
    return await api.get_user_bookshelf(uid, lang)


@app.get("/novel/{aid}/comments", tags=["comments"], response_model=list[CommentItem])
async def comments(
    aid: Annotated[int, Path(description="文章 ID")],
    api: ApiDep,
    page: Annotated[int, Query(ge=1)] = 1,
    lang: LangQ = Lang.zh_CN,
):
    return await api.get_comments(aid, page, lang)


@app.get("/comments/{rid}/replies", tags=["comments"], response_model=list[ReplyItem])
async def replies(
    rid: Annotated[int, Path(description="书评 ID")],
    api: ApiDep,
    page: Annotated[int, Query(ge=1)] = 1,
    lang: LangQ = Lang.zh_CN,
):
    return await api.get_replies(rid, page, lang)


@app.get("/user/info", tags=["user"], response_model=UserInfo)
async def user_info(api: ApiDep, lang: LangQ = Lang.zh_CN):
    return await api.get_user_info(lang)


@app.get("/recommend", tags=["novel"], response_model=list[RecommendBlock])
async def recommend(api: ApiDep, lang: LangQ = Lang.zh_CN):
    return await api.get_recommend(lang)


@app.get("/picture", tags=["meta"], response_class=Response)
async def picture(url: Annotated[str, Query(description="wenku8 域内图片直链 URL")], api: ApiDep):
    """图片透传（httpx 直取 CDN，JPEG）。仅允许 wenku8 域名，非法域返回 400。"""
    data = await api.get_picture(url)
    return Response(content=data, media_type="image/jpeg")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "wenku8.server:app",
        host=os.environ.get("WENKU8_HOST", "127.0.0.1"),
        port=int(os.environ.get("WENKU8_PORT", "8000")),
        reload=False,
    )
