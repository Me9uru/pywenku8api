import asyncio
import functools
import re
import time
from urllib.parse import quote

import httpx
import lxml.html
import zendriver
from lxml import etree

from wenku8.consts import LoginValidity, Lang, SearchMethod, NovelSortMethod
from wenku8.exceptions import NotLoggedInException, CloudflareChallengeException, PageParseError, RateLimitException
from wenku8.models import NovelInfo, _Volume, _Chapter, NovelIndex, SearchItem, SearchResult, PageControl, BookshelfItem, NovelCover, CommentItem, ReplyItem, RecommendBlock, UserInfo
from wenku8.utils import extract_text, cooldown, separate_chinese_colon, get_chapter_content, lang_convent


def login_required(func):
    @functools.wraps(func)
    async def wrapper(self, *args, **kwargs):
        if not self.is_logged_in:
            raise NotLoggedInException
        return await func(self, *args, **kwargs)

    return wrapper


class Wenku8API:
    ENDPOINT = "https://www.wenku8.net"
    # CDN 二进制资源仅校验 UA，用真实浏览器 UA 直连即可
    _USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
    # 整本下载短时缓存：30 分钟，减少重复下载
    _FULL_CONTENT_CACHE_TTL = 30 * 60

    def __init__(self, endpoint: str = "https://www.wenku8.net", headless: bool = True,
                 proxy: str | None = None):
        """初始化 Wenku8API。

        proxy: SOCKS5/HTTP 代理 URL，如 "socks5h://127.0.0.1:1080"。同时作用于
               httpx 下载与 zendriver 浏览器。None 表示直连。
        """
        self.ENDPOINT = endpoint
        self.headless = headless
        self.proxy = proxy
        # 常驻浏览器，懒启动；_browser_lock 仅保护启动，_nav_lock 串行化页面导航
        self._browser = None
        self._browser_lock = asyncio.Lock()
        self._nav_lock = asyncio.Lock()
        self._phpsessid: str | None = None
        # 整本下载内存缓存：{(aid, lang): (expire_time, content)}
        self._full_content_cache: dict[tuple[int, "Lang"], tuple[float, str]] = {}

    async def _ensure_browser(self):
        """懒启动常驻浏览器（双重检查锁）。必须在 _nav_lock 之外调用以避免嵌套死锁。"""
        if self._browser is None:
            async with self._browser_lock:
                if self._browser is None:
                    browser_args = None
                    if self.proxy:
                        # Chromium 的 --proxy-server 不识别 socks5h scheme，须转成 socks5。
                        # Chromium 的 SOCKS5 默认在代理端解析 DNS，等效 socks5h。
                        chrome_proxy = self.proxy.replace("socks5h://", "socks5://")
                        browser_args = [f"--proxy-server={chrome_proxy}"]
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

    async def _refresh_cookies(self, browser) -> None:
        """从浏览器 cookie jar 同步 PHPSESSID 到 self._phpsessid。"""
        for cookie in await browser.cookies.get_all():
            if cookie.name == "PHPSESSID":
                self._phpsessid = cookie.value
                return

    async def _navigate(self, url: str, *, want_url: bool = False):
        """导航到 url，处理 Cloudflare 质询，返回剥离 tbody 后的渲染 HTML。

        渲染后的 DOM 已是正确解码的 Unicode（规避了 CDP getResponseBody 对 GBK 的误解码），
        调用方直接 etree.HTML(html) 即可，无需再设置 encoding。
        """
        browser = await self._ensure_browser()
        async with self._nav_lock:
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
            await self._refresh_cookies(browser)
            if want_url:
                final_url = await tab.evaluate("window.location.href")
                return html, final_url
            return html

    async def _fetch_binary(self, url: str) -> bytes:
        """通过 httpx 直接抓取二进制资源（封面图、整本 TXT 等）。

        二进制资源位于 CDN（img.wenku8.com / dlN.wenku8.com），正常仅需浏览器 UA
        即可通过。但 CDN 可能配置 Cloudflare 防火墙：httpx 的 TLS 指纹与真实浏览器
        不同，遇到质询时无法通过。检测到质询响应（非 200 或质询页）即抛
        CloudflareChallengeException，不尝试浏览器质询。
        """
        async with httpx.AsyncClient(
            headers={"User-Agent": self._USER_AGENT},
            follow_redirects=True,
            timeout=30.0,
            proxy=self.proxy,
        ) as client:
            resp = await client.get(url)
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
        return self._phpsessid

    @property
    def is_logged_in(self):
        return bool(self._phpsessid)

    async def get_novel_cover(self, aid: int):
        return await self._fetch_binary(f"https://img.wenku8.com/image/{int(aid) // 1000}/{aid}/{aid}s.jpg")

    @login_required
    async def get_novel_info(self, aid: int, lang: Lang = Lang.zh_CN) -> NovelInfo:
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
        ), lang)

    @login_required
    async def get_novel_index(self, aid: int, lang: Lang = Lang.zh_CN) -> NovelIndex:
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
                                       volumes=volumes), lang)

    @login_required
    async def get_novel_content(self, aid: int, cid: int, lang: Lang = Lang.zh_CN) -> str:
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

        return lang_convent("".join(results), lang)

    async def get_full_novel_content(self, aid: int, lang: Lang = Lang.zh_CN) -> str:
        """下载整本小说（UTF-8 TXT）。直接访问 CDN 静态文件，绕开 dl.wenku8.com 的 Cloudflare 质询。

        CDN 有多个节点（dl1/dl2），单个节点可能返回 429 限流，逐节点回退重试。
        结果按 (aid, lang) 做 30 分钟内存缓存，避免 get_novel_content_via_full
        反复下载同一本整本。
        """
        cache_key = (aid, lang)
        now = time.monotonic()
        cached = self._full_content_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

        last_err = None
        for node in (1, 2):
            url = f"https://dl{node}.wenku8.com/txtutf8/{int(aid) // 1000}/{aid}.txt"
            try:
                body = await self._fetch_binary(url)
                content = lang_convent(body.decode("utf-8"), lang)
                self._full_content_cache[cache_key] = (now + self._FULL_CONTENT_CACHE_TTL, content)
                # 惰性清理：写新缓存时顺带移除已过期条目，防止缓存无限增长
                for k, (expire, _) in list(self._full_content_cache.items()):
                    if expire <= now:
                        del self._full_content_cache[k]
                return content
            except httpx.HTTPStatusError as e:
                last_err = e
                if e.response.status_code != 429:
                    raise
        raise last_err

    @login_required
    async def get_novel_content_via_full(self, aid: int, cid: int, lang: Lang = Lang.zh_CN) -> str:
        full_content = await self.get_full_novel_content(aid, lang)
        novel_index = await self.get_novel_index(aid, lang)
        return get_chapter_content(full_content, novel_index, cid)

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
            results.append(SearchItem(aid=re.search(r'(\d+).htm', link.get("href")).group(1),
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
    @cooldown(5)
    async def search_novel(self, keyword: str, method: SearchMethod, page: int = 1,
                           lang: Lang = Lang.zh_CN) -> SearchResult:
        keyword = lang_convent(keyword, Lang.zh_CN)
        html, final_url = await self._navigate(
            self.ENDPOINT + f"/modules/article/search.php?searchtype={method}&searchkey={quote(keyword.encode('gbk'))}&page={page}",
            want_url=True)
        if final_url.endswith(".htm"):  # 只有一个结果时会跳转到对应的页面
            info = await self.get_novel_info(re.search(r"(\d*).htm", final_url).group(1), lang=lang)
            return lang_convent(SearchResult(
                results=[SearchItem(aid=info.aid, title=info.title, author=info.author, press=info.press,
                                    last_updated=info.last_updated, word_count=info.word_count,
                                    status=info.status, tags=info.tags, intro_preview=info.intro,
                                    copyright=info.copyright, animation=info.animation)],
                page_control=PageControl(now=1, previous=1, next=1, begin=1, end=1)), lang)
        else:
            parser = etree.HTML(html)
            return lang_convent(self._search_page_parser(html, parser), lang)

    async def search_novel_by_name(self, keyword: str, page: int = 1, lang: Lang = Lang.zh_CN):
        return await self.search_novel(keyword, SearchMethod.NAME, page, lang)

    async def search_novel_by_author(self, keyword: str, page: int = 1, lang: Lang = Lang.zh_CN):
        return await self.search_novel(keyword, SearchMethod.AUTHOR, page, lang)

    async def get_picture(self, url: str):
        return await self._fetch_binary(url)

    @login_required
    async def get_novel_list(self, sort: NovelSortMethod, page: int = 1, lang: Lang = Lang.zh_CN) -> SearchResult:
        html = await self._navigate(
            self.ENDPOINT + f"/modules/article/toplist.php?sort={sort}&page={page}&charset=gbk")
        parser = etree.HTML(html)
        return lang_convent(self._search_page_parser(html, parser), lang)

    @login_required
    async def get_bookshelf(self, bid: int = 0, lang: Lang = Lang.zh_CN) -> list[BookshelfItem]:
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

        return lang_convent(results, lang)

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
    async def get_novel_by_category(self, tag: str, sort: str, page: int = 1,
                                    lang: Lang = Lang.zh_CN) -> SearchResult:
        """按分类(tag)获取小说列表。sort 为 wenku8 排序键（如 lastupdate/allvisit）。"""
        tag = lang_convent(tag, Lang.zh_CN)
        encoded = quote(tag.encode('gbk'))
        html = await self._navigate(
            self.ENDPOINT + f"/modules/article/tags.php?t={encoded}&v={sort}&page={page}&charset=gbk")
        parser = etree.HTML(html)
        return lang_convent(self._search_page_parser(html, parser), lang)

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
        html = await self._navigate(
            self.ENDPOINT + f"/modules/article/articlelist.php?fullflag=1&page={page}&charset=gbk")
        parser = etree.HTML(html)
        return lang_convent(self._card_list_parser(html, parser), lang)

    @login_required
    async def add_to_bookshelf(self, aid: int, lang: Lang = Lang.zh_CN) -> str:
        """加入书架，返回页面提示文本。"""
        html = await self._navigate(
            self.ENDPOINT + f"/modules/article/addbookcase.php?bid={aid}&charset=gbk")
        return lang_convent(self._block_content_text(html), lang)

    @login_required
    async def remove_from_bookshelf(self, bid: int) -> bool:
        """从书架移除（bid 为该书在书架中的 id）。

        delid 后 wenku8 重定向到用户中心页（无明确提示文本），故不返回提示，
        仅在出现错误页时抛 PageParseError，否则返回 True。
        """
        # bookcase.php 不支持 charset 参数（见 get_bookshelf，commit 42dad78）
        html = await self._navigate(self.ENDPOINT + f"/modules/article/bookcase.php?delid={bid}")
        if self._is_error_page(html):
            raise PageParseError("移除书架失败，wenku8 返回错误页", html)
        return True

    @login_required
    async def vote_novel(self, aid: int, lang: Lang = Lang.zh_CN) -> str:
        """为小说投票，返回页面提示文本。"""
        html = await self._navigate(
            self.ENDPOINT + f"/modules/article/uservote.php?id={aid}&charset=gbk")
        return lang_convent(self._block_content_text(html), lang)

    @login_required
    async def get_user_bookshelf(self, uid: int, lang: Lang = Lang.zh_CN) -> list[NovelCover]:
        """获取其他用户收藏的书籍。"""
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
        return lang_convent(results, lang)

    @login_required
    async def get_comments(self, aid: int, page: int = 1,
                           lang: Lang = Lang.zh_CN) -> list[CommentItem]:
        """获取书籍评论区。"""
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
        return lang_convent(results, lang)

    @login_required
    async def get_replies(self, rid: int, page: int = 1,
                          lang: Lang = Lang.zh_CN) -> list[ReplyItem]:
        """获取书评的回复列表。"""
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
        return lang_convent(results, lang)

    @login_required
    async def get_user_info(self, lang: Lang = Lang.zh_CN) -> UserInfo:
        """获取当前登录用户详情。"""
        html = await self._navigate(self.ENDPOINT + "/userdetail.php?charset=gbk")
        parser = etree.HTML(html)
        tables = parser.xpath('//*[@id="content"]//table')
        if not tables:
            raise PageParseError("用户信息页面缺少 #content//table",
                                 html, xpath='//*[@id="content"]//table')
        rows = tables[0].xpath('.//tr')

        def cell(row_idx: int, td_idx: int) -> str:
            if row_idx >= len(rows):
                return ""
            tds = rows[row_idx].xpath('./td')
            if td_idx >= len(tds):
                return ""
            return "".join(tds[td_idx].itertext()).strip()

        avatar = ""
        if rows:
            imgs = rows[0].xpath('./td[3]//img')
            if imgs:
                avatar = (imgs[0].get("src") or "").replace("https", "http")
        uid_str = cell(0, 1)
        email = ""
        if len(rows) > 7:
            a = rows[7].xpath('.//a')
            if a:
                email = (a[0].text or "").strip()
        return lang_convent(UserInfo(
            avatar=avatar,
            uid=int(uid_str) if uid_str.isdigit() else 0,
            username=cell(2, 1),
            user_level=cell(4, 1),
            email=email,
            register_date=cell(12, 1),
            contribution=cell(13, 1),
            experience=cell(14, 1),
            point=cell(15, 1),
            max_bookshelf_num=cell(18, 1),
            max_recommend_num=cell(19, 1)), lang)

    @login_required
    async def get_recommend(self, lang: Lang = Lang.zh_CN) -> list[RecommendBlock]:
        """获取首页推荐区块。"""
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
        return lang_convent(results, lang)
