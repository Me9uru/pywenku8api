import asyncio
import functools
import os
import re
import time
from urllib.parse import parse_qs, quote, urlparse, urlsplit, urlunsplit

import httpx
import lxml.html
import zendriver
from zendriver.cdp.network import CookieParam
from lxml import etree

from wenku8.consts import LoginValidity, Lang, SearchMethod, NovelSortMethod
from wenku8.cache import AsyncCache, CachePolicy
from wenku8.exceptions import NotLoggedInException, CloudflareChallengeException, PageParseError, RateLimitException, InvalidUrlError
from wenku8.models import NovelInfo, _Volume, _Chapter, NovelIndex, SearchItem, SearchResult, PageControl, BookshelfItem, NovelCover, CommentItem, ReplyItem, RecommendBlock, UserInfo
from wenku8.utils import extract_text, separate_chinese_colon, get_chapter_content, lang_convent


def login_required(func):
    @functools.wraps(func)
    async def wrapper(self, *args, **kwargs):
        if not self.is_logged_in:
            raise NotLoggedInException
        return await func(self, *args, **kwargs)

    return wrapper


class Wenku8API:
    ENDPOINT = "https://www.wenku8.cc"
    # CDN 二进制资源仅校验 UA，用真实浏览器 UA 直连即可
    _USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
    _CACHE_POLICIES = {
        "info": CachePolicy(10 * 60, 6 * 60 * 60),
        "index": CachePolicy(30 * 60, 24 * 60 * 60),
        # 大正文不做 stale 后台刷新，避免刷新瞬间同时持有新旧两份文本。
        "chapter": CachePolicy(24 * 60 * 60, stale_while_revalidate=False),
        "full": CachePolicy(6 * 60 * 60, stale_while_revalidate=False),
        "search": CachePolicy(10 * 60, 60 * 60),
        "list": CachePolicy(10 * 60, 60 * 60),
        "category": CachePolicy(15 * 60, 2 * 60 * 60),
        "finished": CachePolicy(30 * 60, 6 * 60 * 60),
        "recommend": CachePolicy(10 * 60, 60 * 60),
        "comments": CachePolicy(60, 10 * 60),
        "user_bookshelf": CachePolicy(5 * 60, 60 * 60),
        # 登录用户数据只放内存，且不后台返回过期值后刷新。
        "private_bookshelf": CachePolicy(15, 2 * 60, stale_while_revalidate=False),
        "private_user": CachePolicy(5 * 60, 30 * 60, stale_while_revalidate=False),
    }

    def __init__(self, endpoint: str = "https://www.wenku8.cc", headless: bool | None = None,
                 proxy: str | None = None, *, cache: AsyncCache | None = None,
                 cache_memory_bytes: int = 64 * 1024 * 1024,
                 http_client: httpx.AsyncClient | None = None):
        """初始化 Wenku8API。

        headless: 是否无头模式。None（默认）时读 WENKU8_HEADLESS 环境变量（取
                  "0"/"false"/"no"/"off"/"headed" 为有头，其余或缺省为无头）；
                  显式传值优先于环境变量。有头模式可显著降低 CF 限流频率，但需 X 显示。
        proxy: SOCKS5/HTTP 代理 URL，如 "socks5h://127.0.0.1:1080"。同时作用于
               httpx 下载与 zendriver 浏览器。None 表示直连。
        http_client: 可选的外部 AsyncClient（主要用于测试/高级定制）；其生命周期
                     由调用方负责，且应启用重定向跟随。
        """
        self.ENDPOINT = endpoint.rstrip("/")
        # 显式传值优先；否则读 WENKU8_HEADLESS 环境变量（便于用 .env 切换），缺省无头。
        if headless is None:
            env = os.environ.get("WENKU8_HEADLESS", "").strip().lower()
            self.headless = env not in ("0", "false", "no", "off", "headed")
        else:
            self.headless = headless
        self.proxy = proxy
        # 常驻浏览器，懒启动；_browser_lock 仅保护启动，_nav_lock 串行化页面导航
        self._browser = None
        self._browser_lock = asyncio.Lock()
        self._nav_lock = asyncio.Lock()
        self._phpsessid: str | None = None
        self._auth_generation = 0
        self._http = http_client or httpx.AsyncClient(
            headers={"User-Agent": self._USER_AGENT},
            follow_redirects=True,
            timeout=30.0,
            proxy=self.proxy,
            # proxy=None 明确表示直连，与 Chromium 的 --no-proxy-server 对齐。
            trust_env=False,
        )
        self._owns_http = http_client is None
        self.cache = cache or AsyncCache(memory_max_bytes=cache_memory_bytes)
        self._owns_cache = cache is None

    async def _ensure_browser(self):
        """懒启动常驻浏览器（双重检查锁）。必须在 _nav_lock 之外调用以避免嵌套死锁。"""
        if self._browser is None:
            async with self._browser_lock:
                if self._browser is None:
                    # proxy=None 的公开契约是直连。显式关闭 Chromium 的系统/环境
                    # 代理，避免 zendriver 的临时 profile 意外继承桌面代理设置。
                    browser_args = ["--no-proxy-server"]
                    if self.proxy:
                        # Chromium 的 --proxy-server 不识别 socks5h scheme，须转成 socks5。
                        # Chromium 的 SOCKS5 默认在代理端解析 DNS，等效 socks5h。
                        chrome_proxy = self.proxy.replace("socks5h://", "socks5://")
                        browser_args = [f"--proxy-server={chrome_proxy}"]
                    # 有头模式必须有图形显示：无 DISPLAY 时 zendriver 会抛误导性的
                    # "Failed to connect to browser / running as root"，此处提前给出明确指引。
                    if not self.headless and not os.environ.get("DISPLAY"):
                        raise RuntimeError(
                            "headless=False 需要 X 图形显示，但 DISPLAY 环境变量未设置。"
                            "本地有 X 会话请 `export DISPLAY=:0`；无显示环境（服务器）请用 "
                            "`xvfb-run -a python ...` 或安装 Xvfb 后 `export DISPLAY=:99`。"
                        )
                    self._browser = await zendriver.start(
                        config=zendriver.Config(headless=self.headless, sandbox=False,
                                                browser_args=browser_args))
        return self._browser

    async def close(self):
        """关闭常驻浏览器，释放资源。"""
        if self._browser is not None:
            await self._browser.stop()
            self._browser = None
            self._phpsessid = None
        if self._owns_http:
            await self._http.aclose()
        if self._owns_cache:
            await self.cache.close()

    async def cache_stats(self) -> dict:
        """Return cache hit/miss, size and in-flight request metrics."""
        return await self.cache.stats()

    @property
    def _private_cache_namespace(self) -> str:
        return f"private:{self._auth_generation}"

    @staticmethod
    def _from_canonical(value, lang: Lang):
        """Convert cached simplified data only when the caller wants Big5.

        Returning the canonical object directly for zh_CN is important for
        large full-book strings: ``str.translate`` would briefly allocate a
        second copy on every cache hit.
        """
        # Strings are immutable and can be shared safely. Structured results
        # still pass through lang_convent, which rebuilds dataclasses/lists so
        # a caller cannot mutate the cached canonical object.
        if lang == Lang.zh_CN and isinstance(value, str):
            return value
        return lang_convent(value, lang)

    @staticmethod
    def _strip_tbody(html: str) -> str:
        """浏览器渲染后的 DOM 会被注入 <tbody>，破坏现有不带 tbody 的 XPath，故剥离。"""
        return re.sub(r"</?tbody[^>]*>", "", html, flags=re.IGNORECASE)

    @staticmethod
    def _is_cf_challenge(html: str) -> bool:
        """判断是否为 Cloudflare 质询/拦截页（含质询与封禁两类）。

        - 质询页（可尝试 verify_cf 解决）："Just a moment" / "Attention Required!"
          / 中文「请稍候…」/ "正在进行安全验证" / _cf_chl_opt / "正在验证您是否是真人"
        - 封禁页（不可解决，IP 被限流/拦截）："Access denied" / "used Cloudflare to
          restrict access" / "errorCode" / "cf-error-details" / 错误码 1015
        """
        head = html[:4096].lower()
        return ("<title>just a moment" in head
                or "<title>attention required" in head
                or "<title>请稍候" in head
                or "正在进行安全验证" in head
                or "_cf_chl_opt" in head
                or "正在验证您是否是真人" in head
                or "access denied" in head
                or "used cloudflare to restrict access" in head
                or "errorcode" in head
                or "cf-error-details" in head
                or "cf-error-code" in head)

    @staticmethod
    def _is_cf_blocked(html: str) -> bool:
        """判断是否为 Cloudflare 封禁页（IP 被限流/拦截，无法通过质询解决）。"""
        head = html[:4096].lower()
        return ("access denied" in head
                or "used cloudflare to restrict access" in head
                or "cf-error-details" in head
                or "cf-error-code" in head
                or "errorcode" in head)

    async def _wait_cf(self, tab, timeout: float = 60.0) -> str:
        """等待页面加载完成并处理 Cloudflare 质询/封禁，返回最终 HTML。

        tab.get() 对 wenku8 常在 body 渲染前就返回，故先等 readyState=complete，
        否则会拿到只有 <head> 的空壳页面。

        - 仅当页面确认为 CF 质询页（_is_cf_challenge 且非封禁）才调 verify_cf 解决，
          正常页面直接返回，避免无谓的等待。
        - CF 封禁页（IP 被限流/拦截，如错误码 1015）无法通过质询解决，立即抛
          RateLimitException（携带页面内容）而非空转至超时。
        """
        try:
            await tab.wait_for_ready_state("complete", timeout=15)
        except Exception:
            pass
        deadline = time.monotonic() + timeout
        html = await tab.get_content()
        while self._is_cf_challenge(html) and time.monotonic() < deadline:
            if self._is_cf_blocked(html):
                raise RateLimitException(f"Cloudflare 封禁/IP 限流: {html[:2000]}")
            try:
                await tab.verify_cf()
            except Exception:
                pass
            await asyncio.sleep(2)
            # verify_cf 可能已解决质询：若页面恢复，等 readyState=complete 确保
            # 目标内容完全加载后再返回，避免拿到半加载的中间页
            if not self._is_cf_challenge(await tab.get_content()):
                try:
                    await tab.wait_for_ready_state("complete", timeout=15)
                except Exception:
                    pass
                return await tab.get_content()
            try:
                await tab.reload()
            except Exception:
                pass
            try:
                await tab.wait_for_ready_state("complete", timeout=15)
            except Exception:
                pass
            html = await tab.get_content()
        if self._is_cf_blocked(html):
            raise RateLimitException(f"Cloudflare 封禁/IP 限流: {html[:2000]}")
        if self._is_cf_challenge(html):
            # 质询在 timeout 内未解决：返回质询页只会让调用方解析崩溃，
            # 不如抛明确异常，由上层决定重试。
            raise TimeoutError("Cloudflare 质询在限时内未解决")
        return html

    @staticmethod
    def _node_candidates(url: str) -> list[str]:
        """为 wenku8.cc/.net 生成主、备节点 URL；自定义 endpoint 不改写。"""
        parts = urlsplit(url)
        if parts.hostname not in ("www.wenku8.cc", "www.wenku8.net"):
            return [url]
        alternate = "www.wenku8.net" if parts.hostname == "www.wenku8.cc" else "www.wenku8.cc"
        alt_netloc = alternate
        if parts.port:
            alt_netloc += f":{parts.port}"
        return [url, urlunsplit((parts.scheme, alt_netloc, parts.path, parts.query, parts.fragment))]

    @staticmethod
    def _decode_html(response: httpx.Response) -> str:
        """按显式 charset/响应头解码原始页面；wenku8 的 gbk 用 GB18030 超集。"""
        query_charset = parse_qs(response.url.query.decode("ascii", "ignore")).get("charset", [""])[0].lower()
        header_charset = (response.charset_encoding or "").lower()
        charset = query_charset or header_charset
        if "big5" in charset:
            encoding = "big5hkscs"
        elif charset in ("gbk", "gb2312", "gb18030"):
            encoding = "gb18030"
        else:
            # bookcase.php 不能附加 charset，但站点默认仍返回 GBK 页面。
            encoding = "gb18030" if response.url.host.endswith(("wenku8.cc", "wenku8.net")) else "utf-8"
        return response.content.decode(encoding, "replace")

    async def _request_html(self, url: str, *, want_url: bool = False,
                            allow_node_fallback: bool = True):
        """HTTP 主路径：节点回退、一次 429 退避重试、CF 分类及正确字符集解码。"""
        challenges: list[tuple[str, str]] = []
        failures: list[Exception] = []
        candidates = self._node_candidates(url) if allow_node_fallback else [url]
        for candidate in candidates:
            try:
                response = None
                for attempt in range(2):
                    response = await self._http.get(candidate)
                    if response.status_code != 429 or attempt:
                        break
                    retry_after = response.headers.get("Retry-After", "1")
                    try:
                        delay = min(max(float(retry_after), 0.0), 5.0)
                    except ValueError:
                        delay = 1.0
                    await asyncio.sleep(delay)
                assert response is not None
                probe = response.content[:4096].decode("utf-8", "ignore")
                if self._is_cf_blocked(probe):
                    failures.append(RateLimitException(
                        f"Cloudflare 封禁/IP 限流: {candidate} (HTTP {response.status_code})"))
                    continue
                cf_mitigated = response.headers.get("cf-mitigated", "").lower()
                if self._is_cf_challenge(probe) or "challenge" in cf_mitigated:
                    challenges.append((candidate, probe))
                    continue
                if response.status_code == 403:
                    failures.append(RateLimitException(
                        f"Cloudflare 拒绝访问: {candidate} (HTTP 403)"))
                    continue
                if response.status_code == 429:
                    failures.append(RateLimitException(f"上游限流: {candidate} (HTTP 429)"))
                    continue
                if response.status_code >= 500:
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        failures.append(exc)
                    continue
                response.raise_for_status()
                html = self._strip_tbody(self._decode_html(response))
                if want_url:
                    return html, str(response.url)
                return html
            except httpx.TransportError as exc:
                failures.append(exc)

        if challenges:
            exc = CloudflareChallengeException(
                f"HTTP 路径遇到 Cloudflare 质询: {challenges[0][0]}")
            exc.url = challenges[0][0]
            raise exc
        if failures:
            raise failures[-1]
        raise RuntimeError(f"没有可用的 Wenku8 节点: {url}")

    async def _sync_http_cookies_from_browser(self, browser) -> None:
        """把浏览器的完整 Cookie 集合复制给 HTTP 主路径，并刷新登录态。"""
        for cookie in await browser.cookies.get_all():
            domain = cookie.domain or urlparse(self.ENDPOINT).hostname
            domains = [domain]
            if domain and domain.lstrip(".") in ("wenku8.cc", "www.wenku8.cc",
                                                  "wenku8.net", "www.wenku8.net"):
                # Wenku8 两个节点共享账号 Cookie；与 hikari 的双节点写入策略一致。
                domains = [".wenku8.cc", ".wenku8.net"]
            for target_domain in domains:
                self._http.cookies.set(
                    cookie.name, cookie.value, domain=target_domain,
                    path=cookie.path or "/",
                )
            if cookie.name == "PHPSESSID":
                self._phpsessid = cookie.value

    async def _sync_browser_cookies_from_http(self, browser) -> None:
        """在浏览器兜底前复制 HTTP 路径收到的新 Cookie。"""
        params = []
        for cookie in self._http.cookies.jar:
            params.append(CookieParam(
                name=cookie.name,
                value=cookie.value,
                domain=cookie.domain or None,
                path=cookie.path or "/",
                secure=bool(cookie.secure),
            ))
        if params:
            await browser.cookies.set_all(params)

    async def _refresh_cookies(self, browser) -> None:
        """兼容旧内部调用：现在同步完整 Cookie，而不只是 PHPSESSID。"""
        await self._sync_http_cookies_from_browser(browser)

    async def _navigate_browser(self, url: str, *, want_url: bool = False):
        """仅用于登录或 HTTP 遇到可解决的 CF 质询时的 Chromium 兜底。"""
        browser = await self._ensure_browser()
        async with self._nav_lock:
            await self._sync_browser_cookies_from_http(browser)
            tab = browser.main_tab
            await tab.get(url)
            # 不要无条件调 verify_cf：它会对非质询页空等 15s 超时。
            # _wait_cf 内部已用 _is_cf_challenge 判断，只在真遇质询时处理。
            html = self._strip_tbody(await self._wait_cf(tab))
            # 兜底：任何情况下都不把 CF 封禁/质询页返回给解析方法
            if self._is_cf_blocked(html):
                raise RateLimitException(f"Cloudflare 封禁/IP 限流: {html[:2000]}")
            if self._is_cf_challenge(html):
                raise TimeoutError("Cloudflare 质询在限时内未解决")
            await self._sync_http_cookies_from_browser(browser)
            if want_url:
                final_url = await tab.evaluate("window.location.href")
                return html, final_url
            return html

    async def _navigate(self, url: str, *, want_url: bool = False,
                        allow_node_fallback: bool = True):
        """优先直接 HTTP；仅在两个节点都遇到可解决的 CF 质询时启动浏览器。"""
        try:
            return await self._request_html(
                url, want_url=want_url, allow_node_fallback=allow_node_fallback)
        except CloudflareChallengeException as exc:
            fallback_url = getattr(exc, "url", url)
            return await self._navigate_browser(fallback_url, want_url=want_url)

    async def _fetch_binary(self, url: str) -> bytes:
        """通过 httpx 直接抓取二进制资源（封面图、整本 TXT 等）。

        二进制资源位于 CDN（img.wenku8.com / dlN.wenku8.com），正常仅需浏览器 UA
        即可通过。但 CDN 可能配置 Cloudflare 防火墙：httpx 的 TLS 指纹与真实浏览器
        不同，遇到质询时无法通过。检测到质询响应（非 200 或质询页）即抛
        CloudflareChallengeException，不尝试浏览器质询。
        """
        resp = await self._http.get(url)
        # CF 质询页：HTTP 403/503 且响应体是质询页 → 明确错误
        if self._is_cf_challenge(resp.content[:4096].decode("utf-8", "ignore")):
            raise CloudflareChallengeException(
                f"CDN 资源被 Cloudflare 防火墙拦截: {url} (HTTP {resp.status_code})")
        # 其他非 200（含 429，由 get_full_novel_content 做节点回退）
        resp.raise_for_status()
        return resp.content

    async def login(self, username: str, password: str, validity: LoginValidity = LoginValidity.NONE) -> str:
        """访问登录页面，自动填充用户名密码并点击提交完成登录，返回 PHPSESSID。"""
        browser = await self._ensure_browser()
        async with self._nav_lock:
            tab = browser.main_tab
            # 裸 login.php 的 body 为空，登录表单在 login.php?do=submit
            await tab.get(self.ENDPOINT + "/login.php?do=submit")
            await self._wait_cf(tab)
            await tab.wait_for("input[name=username]", timeout=15)
            await (await tab.select("input[name=username]")).send_keys(username)
            await (await tab.select("input[name=password]")).send_keys(password)
            # usecookie 是 <select>，选项值与 LoginValidity 一致（内部枚举，无注入风险）
            await tab.evaluate(
                f'document.querySelector("select[name=usecookie]").value="{validity.value}"')
            await (await tab.select("input[name=submit]")).click()
            await asyncio.sleep(1)  # click 不自动等待导航完成
            await self._wait_cf(tab)  # 跳转后可能再次遇到 Cloudflare
            await self._refresh_cookies(browser)
        self._auth_generation += 1
        await self.cache.invalidate_namespace_prefix("private:")
        return self._phpsessid

    @property
    def is_logged_in(self):
        return bool(self._phpsessid)

    async def get_novel_cover(self, aid: int):
        aid = int(aid)
        return await self._fetch_binary(
            f"https://img.wenku8.com/image/{aid // 1000}/{aid}/{aid}s.jpg"
        )

    @login_required
    async def get_novel_info(self, aid: int, lang: Lang = Lang.zh_CN) -> NovelInfo:
        aid = int(aid)
        canonical = await self.cache.get_or_load(
            "info", str(aid), self._CACHE_POLICIES["info"],
            lambda: self._get_novel_info_uncached(aid),
        )
        return self._from_canonical(canonical, lang)

    async def _get_novel_info_uncached(self, aid: int) -> NovelInfo:
        html = await self._navigate(self.ENDPOINT + f"/modules/article/articleinfo.php?id={aid}&charset=gbk")
        parser = etree.HTML(html)

        if bool(len(parser.xpath('//*[@id="content"]/div[1]/table[2]/tr/td[2]/span[2]/b/br'))):
            last_updated = None
            word_count = None
            popularity_level = None
            trending_level = None
            latest_section = None
            intro = "".join(parser.xpath('//*[@id="content"]/div[1]/table[2]/tr/td[2]/span[4]//text()'))
        else:
            last_updated = extract_text(parser, '//*[@id="content"]/div[1]/table[1]/tr[2]/td[4]', True)
            word_count_str = extract_text(parser, '//*[@id="content"]/div[1]/table[1]/tr[2]/td[5]', True).replace("字", "")
            word_count = int(word_count_str) if word_count_str else None
            rating_parts = extract_text(
                parser, '//*[@id="content"]/div[1]/table[2]/tr/td[2]/span[2]/b').split("，")
            popularity_level = separate_chinese_colon(rating_parts[0])[1] if rating_parts else None
            trending_level = separate_chinese_colon(rating_parts[1])[1] if len(rating_parts) > 1 else None
            latest_section = extract_text(parser, '//*[@id="content"]/div[1]/table[2]/tr/td[2]/span[4]/a')
            intro = "".join(parser.xpath('//*[@id="content"]/div[1]/table[2]/tr/td[2]/span[6]//text()'))

        return lang_convent(NovelInfo(
            aid=aid,
            title=extract_text(parser, '//*[@id="content"]/div[1]/table[1]/tr[1]/td/table/tr/td[1]/span/b'),
            author=extract_text(parser, '//*[@id="content"]/div[1]/table[1]/tr[2]/td[2]', True),
            status=extract_text(parser, '//*[@id="content"]/div[1]/table[1]/tr[2]/td[3]', True),
            last_updated=last_updated,
            intro=intro,
            tags=extract_text(parser, '//*[@id="content"]/div[1]/table[2]/tr/td[2]/span[1]/b', True).split(" "),
            press=extract_text(parser, '//*[@id="content"]/div[1]/table[1]/tr[2]/td[1]', True),
            word_count=word_count,
            popularity_level=popularity_level,
            trending_level=trending_level,
            latest_section=latest_section,
            copyright=not bool(len(parser.xpath('//*[@id="content"]/div[1]/table[2]/tr/td[2]/span[2]/b/br'))),
            animation=bool(len(parser.xpath('//*[@id="content"]/div[1]/table[2]/tr/td[1]/span/b')))
        ), Lang.zh_CN)

    @login_required
    async def get_novel_index(self, aid: int, lang: Lang = Lang.zh_CN) -> NovelIndex:
        aid = int(aid)
        canonical = await self.cache.get_or_load(
            "index", str(aid), self._CACHE_POLICIES["index"],
            lambda: self._get_novel_index_uncached(aid),
        )
        return self._from_canonical(canonical, lang)

    async def _get_novel_index_uncached(self, aid: int) -> NovelIndex:
        html = await self._navigate(self.ENDPOINT + f"/modules/article/reader.php?aid={aid}&charset=gbk")
        parser = etree.HTML(html)
        volumes = []
        current_vol = None
        xpath_str = '//table[@class="css"]//td[@class="vcss" or @class="ccss"]'
        for td in parser.xpath(xpath_str):
            cls = td.get("class")
            if cls == "vcss":
                if current_vol:
                    volumes.append(current_vol)
                current_vol = _Volume(
                    vid=int(td.get("vid")),
                    title=td.text.strip() if td.text else "",
                    chapters=[]
                )
            elif cls == "ccss":
                if not current_vol:
                    continue
                link = td.find("a")
                if link is None:
                    continue
                href = link.get("href")
                cid = int(re.search(r'cid=(\d+)', href).group(1))
                current_vol.chapters.append(_Chapter(cid=cid, title=link.text))
        if current_vol:
            volumes.append(current_vol)
        return lang_convent(NovelIndex(aid=aid,
                                       title=extract_text(parser, '//*[@id="title"]'),
                                       author=extract_text(parser, '//*[@id="info"]', True),
                                       volumes=volumes), Lang.zh_CN)

    @login_required
    async def get_novel_content(self, aid: int, cid: int, lang: Lang = Lang.zh_CN) -> str:
        aid, cid = int(aid), int(cid)
        canonical = await self.cache.get_or_load(
            "chapter", f"{aid}:{cid}", self._CACHE_POLICIES["chapter"],
            lambda: self._get_novel_content_uncached(aid, cid),
        )
        return self._from_canonical(canonical, lang)

    async def _get_novel_content_uncached(self, aid: int, cid: int) -> str:
        html = await self._navigate(
            self.ENDPOINT + f"/modules/article/reader.php?aid={aid}&cid={cid}&charset=gbk")
        parser = etree.HTML(html)
        content_nodes = parser.xpath('//*[@id="content"]')
        if not content_nodes:
            # 页面异常（CF 质询残留/等待页/404）时无 #content 节点
            raise PageParseError("章节页面缺少 #content 节点", html,
                                 xpath='//*[@id="content"]')
        results = []
        for child in content_nodes[0]:
            if child.tag == 'div':
                href = child[0].get('href')
                results.append(f"<!--image-->{href}<!--image-->")
            else:
                pass
            if child.tail:
                results.append(child.tail)

        return lang_convent("".join(results), Lang.zh_CN)

    async def get_full_novel_content(self, aid: int, lang: Lang = Lang.zh_CN) -> str:
        """下载整本小说（UTF-8 TXT）。直接访问 CDN 静态文件，绕开 dl.wenku8.com 的 Cloudflare 质询。

        CDN 有多个节点（dl1/dl2），单个节点可能返回 429 限流，逐节点回退重试。
        结果以简体标准文本写入有界内存缓存，避免 get_novel_content_via_full
        反复下载同一本整本；繁体仅在返回边界转换，不保存重复副本。
        """
        aid = int(aid)
        canonical = await self.cache.get_or_load(
            "full", str(aid), self._CACHE_POLICIES["full"],
            lambda: self._get_full_novel_content_uncached(aid),
        )
        return self._from_canonical(canonical, lang)

    async def _get_full_novel_content_uncached(self, aid: int) -> str:
        last_err = None
        for node in (1, 2):
            url = f"https://dl{node}.wenku8.com/txtutf8/{int(aid) // 1000}/{aid}.txt"
            try:
                body = await self._fetch_binary(url)
                return lang_convent(body.decode("utf-8"), Lang.zh_CN)
            except httpx.HTTPStatusError as e:
                last_err = e
                if e.response.status_code != 429:
                    raise
        raise last_err

    @login_required
    async def get_novel_content_via_full(self, aid: int, cid: int, lang: Lang = Lang.zh_CN) -> str:
        # 先在标准简体文本上切章，避免繁体请求每次转换整本大文本；目录本身也会
        # 命中缓存，不再为每一章触发浏览器导航。
        full_content = await self.get_full_novel_content(aid, Lang.zh_CN)
        novel_index = await self.get_novel_index(aid, Lang.zh_CN)
        return lang_convent(get_chapter_content(full_content, novel_index, cid), lang)

    def _search_page_parser(self, html: str, parser: lxml.html.Element):
        results = []
        content_nodes = parser.xpath('//*[@id="content"]/table/tr/td')
        if not content_nodes:
            # 页面异常（CF 质询残留/等待页/404）时无内容节点
            raise PageParseError("搜索/列表页面缺少内容节点", html,
                                 xpath='//*[@id="content"]/table/tr/td')
        for novel in content_nodes[0]:
            if len(novel[1][2].text.split("/")) < 3:
                # 版权本，没有最近更新和字数
                last_updated = None
                word_count = None
                status = novel[1][2].text.split("/")[0]
                animation = len(novel[1][2].text.split("/")) == 2
            else:
                last_updated = novel[1][2].text.split("/")[0].split(":")[1]
                word_count = novel[1][2].text.split("/")[1].split(":")[1]
                status = novel[1][2].text.split("/")[2]
                animation = len(novel[1][2].text.split("/")) == 4

            # Wenku8 繁体网页版似乎存在编码问题，部分字符无法正常以 Big5 显示
            # see also: https://www.wenku8.net/modules/article/articleinfo.php?id=4093&charset=big5
            if "/" in novel[1][1].text:
                press = novel[1][1].text.split("/")[1].split(":")[1]
            else:
                press = novel[1][1].text.split("  ")[1].split(":")[1]

            link = novel[1][0][0]
            title_link = link.get("title") or (link.text or "").strip()
            results.append(SearchItem(aid=int(re.search(r'(\d+).htm', link.get("href")).group(1)),
                                      title=title_link,
                                      author=novel[1][1].text.split("/")[0].split(":")[1],
                                      press=press,
                                      last_updated=last_updated,
                                      word_count=word_count,
                                      status=status,
                                      tags=novel[1][3][0].text.split(" "),
                                      intro_preview=novel[1][4].text.split(":", maxsplit=1)[1],
                                      copyright=not novel[1][5].get("class") == "hottext",
                                      animation=animation
                                      ))

        pagestats_nodes = parser.xpath('//*[@id="pagestats"]')
        if not pagestats_nodes or not pagestats_nodes[0].text:
            raise PageParseError("搜索/列表页面缺少 #pagestats 节点", html,
                                 xpath='//*[@id="pagestats"]')
        page_control_str = pagestats_nodes[0].text
        return SearchResult(results=results, page_control=PageControl.from_str(page_control_str))

    @login_required
    async def search_novel(self, keyword: str, method: SearchMethod, page: int = 1,
                           lang: Lang = Lang.zh_CN) -> SearchResult:
        keyword = lang_convent(keyword, Lang.zh_CN)
        page = int(page)
        canonical = await self.cache.get_or_load(
            "search", f"{method.value}:{keyword}:{page}", self._CACHE_POLICIES["search"],
            lambda: self._search_novel_uncached(keyword, method, page),
        )
        return self._from_canonical(canonical, lang)

    async def _search_novel_uncached(self, keyword: str, method: SearchMethod,
                                     page: int) -> SearchResult:
        html, final_url = await self._navigate(
            self.ENDPOINT + f"/modules/article/search.php?searchtype={method}&searchkey={quote(keyword.encode('gbk'))}&page={page}",
            want_url=True)
        if final_url.endswith(".htm"):  # 只有一个结果时会跳转到对应的页面
            info = await self.get_novel_info(
                re.search(r"(\d*).htm", final_url).group(1), lang=Lang.zh_CN
            )
            return lang_convent(SearchResult(
                results=[SearchItem(aid=info.aid, title=info.title, author=info.author, press=info.press,
                                    last_updated=info.last_updated, word_count=info.word_count,
                                    status=info.status, tags=info.tags, intro_preview=info.intro,
                                    copyright=info.copyright, animation=info.animation)],
                page_control=PageControl(now=1, previous=1, next=1, begin=1, end=1)), Lang.zh_CN)
        else:
            parser = etree.HTML(html)
            return lang_convent(self._search_page_parser(html, parser), Lang.zh_CN)

    async def search_novel_by_name(self, keyword: str, page: int = 1, lang: Lang = Lang.zh_CN):
        return await self.search_novel(keyword, SearchMethod.NAME, page, lang)

    async def search_novel_by_author(self, keyword: str, page: int = 1, lang: Lang = Lang.zh_CN):
        return await self.search_novel(keyword, SearchMethod.AUTHOR, page, lang)

    async def get_picture(self, url: str):
        """获取 wenku8 域内图片（封面/插图等）。

        仅允许 wenku8 系列域名，避免把本库当作开放代理（SSRF：否则可经 ?url=
        探测内网/云元数据）。需要抓取任意 URL 的场景应由调用方自行用 httpx 实现。
        """
        host = (urlparse(url).hostname or "").lower()
        if not host.endswith(("wenku8.com", "wenku8.net", "wenku8.cc")):
            raise InvalidUrlError(
                f"get_picture 仅允许 wenku8 域名图片，拒绝: {host or url}")
        return await self._fetch_binary(url)

    @login_required
    async def get_novel_list(self, sort: NovelSortMethod, page: int = 1, lang: Lang = Lang.zh_CN) -> SearchResult:
        page = int(page)
        canonical = await self.cache.get_or_load(
            "list", f"{sort.value}:{page}", self._CACHE_POLICIES["list"],
            lambda: self._get_novel_list_uncached(sort, page),
        )
        return self._from_canonical(canonical, lang)

    async def _get_novel_list_uncached(self, sort: NovelSortMethod, page: int) -> SearchResult:
        html = await self._navigate(
            self.ENDPOINT + f"/modules/article/toplist.php?sort={sort}&page={page}&charset=gbk")
        parser = etree.HTML(html)
        return lang_convent(self._search_page_parser(html, parser), Lang.zh_CN)

    @login_required
    async def get_bookshelf(self, bid: int = 0, lang: Lang = Lang.zh_CN) -> list[BookshelfItem]:
        bid = int(bid)
        canonical = await self.cache.get_or_load(
            self._private_cache_namespace, f"bookshelf:{bid}",
            self._CACHE_POLICIES["private_bookshelf"],
            lambda: self._get_bookshelf_uncached(bid),
        )
        return self._from_canonical(canonical, lang)

    async def _get_bookshelf_uncached(self, bid: int) -> list[BookshelfItem]:
        # bookcase.php 不支持 charset 参数，带上去会被 Cloudflare 拦截
        html = await self._navigate(self.ENDPOINT + f"/modules/article/bookcase.php?classid={bid}")
        parser = etree.HTML(html)
        table_nodes = parser.xpath('//*[@id="checkform"]/table')
        if not table_nodes:
            raise PageParseError("书架页面缺少 #checkform/table", html,
                                 xpath='//*[@id="checkform"]/table')
        results = []
        for novel in table_nodes[0]:
            if novel.get("align") == "center":
                continue
            if len(novel) == 1:
                continue

            updated_after_last_reading = False
            finished = False
            title_elem = novel[1][0]
            if novel[1][0].text == "新":
                updated_after_last_reading = True
                finished = False
                title_elem = novel[1][1]
            if novel[1][0].text.startswith("["):
                finished = True
                title_elem = novel[1][1]
                if novel[1][1].text == "新":
                    updated_after_last_reading = True
                    title_elem = novel[1][2]

            aid = int(re.search(r'aid=(\d+)', title_elem.get("href")).group(1))
            bid = int(re.search(r'bid=(\d+)', title_elem.get("href")).group(1))

            latest_section = novel[3][0].text
            latest_section_cid = int(re.search(r'cid=(\d+)', novel[3][0].get("href")).group(1))

            bookmark = novel[4][0].text
            if bookmark:
                bookmark_cid = int(re.search(r'cid=(\d+)', novel[4][0].get("href")).group(1))
            else:
                bookmark_cid = None

            results.append(BookshelfItem(aid=aid, bid=bid, title=title_elem.text,
                                         author=novel[2][0].text, latest_section=latest_section,
                                         latest_section_cid=latest_section_cid, bookmark=bookmark,
                                         bookmark_cid=bookmark_cid, last_updated=novel[5].text.strip(),
                                         finished=finished, updated_after_last_reading=updated_after_last_reading))

        return lang_convent(results, Lang.zh_CN)

    # ---- 以下为迁移自 hikari_novel_flutter 的 GET 功能（第一批）----

    @staticmethod
    def _is_error_page(html: str) -> bool:
        """检测 wenku8 操作错误页（.blocktitle 文本为「出现错误！」/「出現錯誤！」）。"""
        parser = etree.HTML(html)
        titles = parser.xpath('//*[contains(@class,"blocktitle")]/text()')
        if not titles:
            return False
        return "".join(titles).strip() in ("出现错误！", "出現錯誤！")

    @staticmethod
    def _block_content_text(
            html: str,
            xpath: str = '//*[contains(@class,"blockcontent")]//div[@style="padding:10px"]') -> str:
        """提取操作提示页 .blockcontent 内的提示文本；错误页抛 PageParseError。"""
        if Wenku8API._is_error_page(html):
            raise PageParseError("操作失败，wenku8 返回错误页", html, xpath=xpath)
        parser = etree.HTML(html)
        nodes = parser.xpath(xpath)
        if not nodes:
            return ""
        return "".join(nodes[0].itertext()).strip()

    @staticmethod
    def _parse_recommend_items(items) -> list[NovelCover]:
        """从推荐区块的封面 div 列表提取 NovelCover。"""
        result = []
        for j in items:
            a_tags = j.xpath('./a')
            img_tags = j.xpath('.//img')
            if len(a_tags) < 2 or not img_tags:
                continue
            title = (a_tags[1].text or "").strip()
            img = img_tags[0].get("src") or ""
            if img and not img.startswith("https"):
                img = img.replace("http", "https", 1)
            url = a_tags[0].get("href") or ""
            aid = 0
            if "book/" in url and ".htm" in url:
                try:
                    aid = int(url[url.find("book/") + 5:url.find(".htm")])
                except ValueError:
                    aid = 0
            result.append(NovelCover(title=title, aid=aid, image_url=img))
        return result

    @login_required
    async def get_novel_by_category(self, tag: str, sort: NovelSortMethod, page: int = 1,
                                    lang: Lang = Lang.zh_CN) -> SearchResult:
        """按分类(tag)获取小说列表。

        sort 为 wenku8 排序键（NovelSortMethod，如 lastupdate/allvisit）；用枚举
        而非裸字符串，避免调用方注入额外查询参数（与 get_novel_list 一致）。
        """
        tag = lang_convent(tag, Lang.zh_CN)
        page = int(page)
        canonical = await self.cache.get_or_load(
            "category", f"{sort.value}:{tag}:{page}", self._CACHE_POLICIES["category"],
            lambda: self._get_novel_by_category_uncached(tag, sort, page),
        )
        return self._from_canonical(canonical, lang)

    async def _get_novel_by_category_uncached(
            self, tag: str, sort: NovelSortMethod, page: int) -> SearchResult:
        encoded = quote(tag.encode('gbk'))
        html = await self._navigate(
            self.ENDPOINT + f"/modules/article/tags.php?t={encoded}&v={sort.value}&page={page}&charset=gbk")
        parser = etree.HTML(html)
        return lang_convent(self._search_page_parser(html, parser), Lang.zh_CN)

    def _card_list_parser(self, html: str, parser) -> SearchResult:
        """解析 articlelist 等页面的 373px 卡片列表（每张卡片含封面/书名/作者/字数/状态/标签/简介）。"""
        cards = parser.xpath('//div[contains(@style,"width:373px")]')
        if not cards:
            raise PageParseError("卡片列表页缺少 373px 卡片", html,
                                 xpath='//div[contains(@style,"width:373px")]')
        results = []
        for card in cards:
            a_tags = card.xpath('.//a')
            if not a_tags:
                continue
            aid_match = re.search(r'/book/(\d+)\.htm', a_tags[0].get("href", ""))
            if not aid_match:
                continue
            aid = int(aid_match.group(1))
            title_a = card.xpath('.//b/a')
            title = (title_a[0].text or "").strip() if title_a else ""
            if not title:
                title = a_tags[0].get("tiptitle", "") or ""
            author = press = last_updated = word_count = status = ""
            tags: list[str] = []
            intro_preview = ""
            for p_el in card.xpath('.//p'):
                txt = "".join(p_el.itertext()).strip().replace("：", ":")
                if txt.startswith("作者:"):
                    parts = txt.split("/")
                    author = parts[0].split(":", 1)[1] if ":" in parts[0] else ""
                    if len(parts) > 1:
                        press = parts[1].split(":", 1)[1] if ":" in parts[1] else parts[1]
                elif txt.startswith("更新:"):
                    parts = txt.split("/")
                    last_updated = parts[0].split(":", 1)[1] if ":" in parts[0] else ""
                    if len(parts) > 1:
                        word_count = parts[1].split(":", 1)[1] if ":" in parts[1] else ""
                    status = parts[2] if len(parts) > 2 else ""
                elif txt.startswith("Tags:"):
                    tags_text = txt[5:].strip()
                    tags = tags_text.split(" ") if tags_text else []
                elif txt.startswith("简介:"):
                    intro_preview = txt[3:].strip()
            # 卡片列表不暴露版权/动画化标记：copyright=True 仅为占位（多数完结书可读），
            # animation=False 同理。权威值请用 get_novel_info(aid) 获取。
            results.append(SearchItem(
                aid=aid, title=title, author=author, press=press,
                last_updated=last_updated, word_count=word_count, status=status,
                tags=tags, intro_preview=intro_preview,
                copyright=True, animation=False))
        ps_nodes = parser.xpath('//*[@id="pagestats"]/text()')
        if not ps_nodes or not ps_nodes[0].strip():
            raise PageParseError("卡片列表页缺少 #pagestats", html, xpath='//*[@id="pagestats"]')
        return SearchResult(results=results, page_control=PageControl.from_str(ps_nodes[0]))

    @login_required
    async def get_finished_novels(self, page: int = 1, lang: Lang = Lang.zh_CN) -> SearchResult:
        """获取已完结小说列表。"""
        page = int(page)
        canonical = await self.cache.get_or_load(
            "finished", str(page), self._CACHE_POLICIES["finished"],
            lambda: self._get_finished_novels_uncached(page),
        )
        return self._from_canonical(canonical, lang)

    async def _get_finished_novels_uncached(self, page: int) -> SearchResult:
        html = await self._navigate(
            self.ENDPOINT + f"/modules/article/articlelist.php?fullflag=1&page={page}&charset=gbk")
        parser = etree.HTML(html)
        return lang_convent(self._card_list_parser(html, parser), Lang.zh_CN)

    @login_required
    async def add_to_bookshelf(self, aid: int, lang: Lang = Lang.zh_CN) -> str:
        """加入书架，返回页面提示文本。"""
        html = await self._navigate(
            self.ENDPOINT + f"/modules/article/addbookcase.php?bid={aid}&charset=gbk",
            allow_node_fallback=False)
        await self.cache.invalidate(self._private_cache_namespace, "bookshelf:")
        return lang_convent(self._block_content_text(html), lang)

    @login_required
    async def remove_from_bookshelf(self, bid: int) -> bool:
        """从书架移除（bid 为该书在书架中的 id）。

        delid 后 wenku8 重定向到用户中心页（无明确提示文本），故不返回提示，
        仅在出现错误页时抛 PageParseError，否则返回 True。
        """
        # bookcase.php 不支持 charset 参数（见 get_bookshelf，commit 42dad78）
        html = await self._navigate(
            self.ENDPOINT + f"/modules/article/bookcase.php?delid={bid}",
            allow_node_fallback=False)
        if self._is_error_page(html):
            raise PageParseError("移除书架失败，wenku8 返回错误页", html)
        await self.cache.invalidate(self._private_cache_namespace, "bookshelf:")
        return True

    @login_required
    async def vote_novel(self, aid: int, lang: Lang = Lang.zh_CN) -> str:
        """为小说投票，返回页面提示文本。"""
        html = await self._navigate(
            self.ENDPOINT + f"/modules/article/uservote.php?id={aid}&charset=gbk",
            allow_node_fallback=False)
        await self.cache.invalidate_key("info", str(int(aid)))
        await self.cache.invalidate("recommend")
        await self.cache.invalidate("list")
        return lang_convent(self._block_content_text(html), lang)

    @login_required
    async def get_user_bookshelf(self, uid: int, lang: Lang = Lang.zh_CN) -> list[NovelCover]:
        """获取其他用户收藏的书籍。"""
        uid = int(uid)
        canonical = await self.cache.get_or_load(
            "user_bookshelf", str(uid), self._CACHE_POLICIES["user_bookshelf"],
            lambda: self._get_user_bookshelf_uncached(uid),
        )
        return self._from_canonical(canonical, lang)

    async def _get_user_bookshelf_uncached(self, uid: int) -> list[NovelCover]:
        html = await self._navigate(self.ENDPOINT + f"/userpage.php?uid={uid}&charset=gbk")
        parser = etree.HTML(html)
        trs = parser.xpath('//*[@id="centerm"]//tr')
        if not trs:
            raise PageParseError("他人书架页面缺少 #centerm", html, xpath='//*[@id="centerm"]//tr')
        results = []
        for tr in trs[1:]:  # 跳过表头行
            anchors = tr.xpath('.//a')
            if len(anchors) < 2:
                continue
            title = (anchors[0].text or "").strip()
            bid_match = re.search(r'bid=(\d+)', anchors[1].get("href", ""))
            if not bid_match:
                continue
            results.append(NovelCover(title=title, aid=int(bid_match.group(1))))
        return lang_convent(results, Lang.zh_CN)

    @login_required
    async def get_comments(self, aid: int, page: int = 1,
                           lang: Lang = Lang.zh_CN) -> list[CommentItem]:
        """获取书籍评论区。"""
        aid, page = int(aid), int(page)
        canonical = await self.cache.get_or_load(
            "comments", f"reviews:{aid}:{page}", self._CACHE_POLICIES["comments"],
            lambda: self._get_comments_uncached(aid, page),
        )
        return self._from_canonical(canonical, lang)

    async def _get_comments_uncached(self, aid: int, page: int) -> list[CommentItem]:
        html = await self._navigate(
            self.ENDPOINT + f"/modules/article/reviews.php?aid={aid}&page={page}&charset=gbk")
        parser = etree.HTML(html)
        tables = parser.xpath('//*[@id="content"]//table')
        if not tables:
            raise PageParseError("评论页面缺少 #content", html, xpath='//*[@id="content"]//table')
        if len(tables) < 3:
            raise PageParseError("评论页面 table 数量不足", html, xpath='//*[@id="content"]//table')
        results = []
        for tr in tables[2].xpath('./tr'):
            if tr.get("align"):
                continue
            tds = tr.xpath('./td')
            if len(tds) < 4:
                continue
            a0 = tds[0].xpath('./a')
            if not a0:
                continue
            rid_match = re.search(r'rid=(\d+)', a0[0].get("href", ""))
            rid = int(rid_match.group(1)) if rid_match else 0
            content = (a0[0].text or "").strip()
            view_reply = (tds[1].text or "").strip()
            idx = view_reply.find('/')
            reply_count = view_reply[:idx] if idx > 0 else ""
            view_count = view_reply[idx + 1:] if idx >= 0 else ""
            a2 = tds[2].xpath('./a')
            user_name = (a2[0].text or "").strip() if a2 else ""
            uid_match = re.search(r'uid=(\d+)', a2[0].get("href", "")) if a2 else None
            uid = int(uid_match.group(1)) if uid_match else 0
            time_str = (tds[3].text or "").strip()
            results.append(CommentItem(
                rid=rid, content=content, view_count=view_count, reply_count=reply_count,
                user_name=user_name, uid=uid, time=time_str))
        return lang_convent(results, Lang.zh_CN)

    @login_required
    async def get_replies(self, rid: int, page: int = 1,
                          lang: Lang = Lang.zh_CN) -> list[ReplyItem]:
        """获取书评的回复列表。"""
        rid, page = int(rid), int(page)
        canonical = await self.cache.get_or_load(
            "comments", f"replies:{rid}:{page}", self._CACHE_POLICIES["comments"],
            lambda: self._get_replies_uncached(rid, page),
        )
        return self._from_canonical(canonical, lang)

    async def _get_replies_uncached(self, rid: int, page: int) -> list[ReplyItem]:
        html = await self._navigate(
            self.ENDPOINT + f"/modules/article/reviewshow.php?rid={rid}&page={page}&charset=gbk")
        parser = etree.HTML(html)
        if not parser.xpath('//*[@id="content"]'):
            raise PageParseError("回复页面缺少 #content", html, xpath='//*[@id="content"]')
        tables = parser.xpath('//*[@id="content"]//table')
        results = []
        # 跳过前 3 后 2（对应 hikari: count<4 continue / count==N-1 break）
        for table in tables[3:-2] if len(tables) > 5 else []:
            # 用 .//td 而非 ./td：_strip_tbody 后结构为 table>tr>td，td 非直接子节点
            tds = table.xpath('.//td')
            if len(tds) < 2:
                continue
            user_link = tds[0].xpath('.//a')
            user_name = (user_link[0].text or "").strip() if user_link else ""
            uid_match = re.search(r'uid=(\d+)', user_link[0].get("href", "")) if user_link else None
            uid = int(uid_match.group(1)) if uid_match else 0
            divs = tds[1].xpath('.//div')
            raw_time = ""
            if len(divs) > 1:
                raw_time = "".join(divs[1].itertext()).strip()
                pipe_idx = raw_time.find('|')
                if pipe_idx > 0:
                    raw_time = raw_time[:pipe_idx - 1].strip()
            content = ""
            if len(divs) > 2:
                content = "".join(divs[2].itertext()).strip()
            results.append(ReplyItem(content=content, user_name=user_name, uid=uid, time=raw_time))
        return lang_convent(results, Lang.zh_CN)

    @login_required
    async def get_user_info(self, lang: Lang = Lang.zh_CN) -> UserInfo:
        """获取当前登录用户详情。

        按「标签文本」锚定字段而非硬编码行列索引：wenku8 增删表格行时不会串档，
        最坏情况是某字段返回空串。关键字基于 wenku8 简体中文常见措辞，若站点改用
        其他措辞需在此校准。uid 与 username 同时为空时抛 PageParseError（页面结构
        重大变更的兜底），其余字段缺失返回空串。
        """
        canonical = await self.cache.get_or_load(
            self._private_cache_namespace, "user_info",
            self._CACHE_POLICIES["private_user"], self._get_user_info_uncached,
        )
        return self._from_canonical(canonical, lang)

    async def _get_user_info_uncached(self) -> UserInfo:
        html = await self._navigate(self.ENDPOINT + "/userdetail.php?charset=gbk")
        parser = etree.HTML(html)
        tables = parser.xpath('//*[@id="content"]//table')
        if not tables:
            raise PageParseError("用户信息页面缺少 #content//table",
                                 html, xpath='//*[@id="content"]//table')
        table = tables[0]
        all_rows = table.xpath('.//tr')

        # 把表格解析为有序 (label, value) 对：每行前两个 td 视作「标签:值」
        pairs: list[tuple[str, str]] = []
        for tr in all_rows:
            tds = tr.xpath('./td')
            if len(tds) < 2:
                continue
            label = "".join(tds[0].itertext()).strip()
            if label:
                pairs.append((label, "".join(tds[1].itertext()).strip()))

        def find(*keys: str) -> str:
            """返回首个标签同时包含全部关键字的行的值；找不到返回 ''。"""
            for label, value in pairs:
                if all(k in label for k in keys):
                    return value
            return ""

        # 头像与 uid 位于首行的特定列（非「标签:值」结构），单独提取
        first_tds = all_rows[0].xpath('./td') if all_rows else []
        avatar = ""
        if len(first_tds) >= 3:
            imgs = first_tds[2].xpath('.//img')
            if imgs:
                avatar = (imgs[0].get("src") or "").replace("https", "http", 1)
        uid_str = find("UID") or find("用户ID") or find("编号")
        if not uid_str and len(first_tds) >= 2:
            uid_str = "".join(first_tds[1].itertext()).strip()

        # 邮箱按链接特征（mailto 或文本含 @）定位，不依赖行号
        email = ""
        for a in table.xpath('.//a'):
            if "mailto:" in (a.get("href") or ""):
                email = (a.text or "").strip()
                break
            text = (a.text or "").strip()
            if "@" in text and "." in text:
                email = text
                break

        username = find("用户名") or find("昵称")
        if not uid_str and not username:
            raise PageParseError(
                "用户信息页面字段解析失败（uid 与用户名均为空，疑似页面结构变更）",
                html, xpath='//*[@id="content"]//table//tr')

        return lang_convent(UserInfo(
            avatar=avatar,
            uid=int(uid_str) if uid_str.isdigit() else 0,
            username=username,
            user_level=find("等级"),
            email=email,
            register_date=find("注册"),
            contribution=find("贡献"),
            experience=find("经验"),
            point=find("积分") or find("点数"),
            max_bookshelf_num=find("书架"),
            max_recommend_num=find("推荐")), Lang.zh_CN)

    @login_required
    async def get_recommend(self, lang: Lang = Lang.zh_CN) -> list[RecommendBlock]:
        """获取首页推荐区块。"""
        canonical = await self.cache.get_or_load(
            "recommend", "index", self._CACHE_POLICIES["recommend"],
            self._get_recommend_uncached,
        )
        return self._from_canonical(canonical, lang)

    async def _get_recommend_uncached(self) -> list[RecommendBlock]:
        html = await self._navigate(self.ENDPOINT + "/index.php?charset=gbk")
        parser = etree.HTML(html)
        if not parser.xpath('//*[@id="centers"]'):
            raise PageParseError("推荐页面缺少 #centers", html, xpath='//*[@id="centers"]')
        results = []
        # 精确匹配 class="block"，避免误匹配 blockcontent/blocktitle（含后者的元素无推荐项）
        blocks = parser.xpath(
            '//*[@id="centers"]//div[contains(concat(" ",normalize-space(@class)," ")," block ")]')
        for block in blocks:
            items = block.xpath(
                './/div[@style="float: left;text-align:center;width: 95px; height:155px;overflow:hidden;"]')
            if not items:
                continue
            title = "".join(block.xpath('.//div[contains(@class,"blocktitle")]/text()')).strip()
            if title:
                title = title.split("(")[0].strip()
            results.append(RecommendBlock(title=title, list=self._parse_recommend_items(items)))
        return lang_convent(results, Lang.zh_CN)
