# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`pywenku8api` is an async Python client for the [Wenku8](https://www.wenku8.net) light-novel website. It is built specifically to bypass Wenku8's Cloudflare firewall (the official API is restricted). Built to support [Wenku8-OPDS](https://github.com/WorldObservationLog/wenku8-opds-readme).

Known limitations: Cloudflare bypass can fail; copyrighted books are unreadable; Japanese IPs are blocked by the site.

## Commands

uv project (Python 3.11+). `uv.lock` is the only dependency lockfile.

```bash
uv sync --locked
.venv/bin/python -m unittest discover -s tests -v
env DISPLAY=:0 WENKU8_HEADLESS=0 .venv/bin/python -m wenku8.server
```

Local dev uses a `.env` (gitignored) loaded via `python-dotenv` with `WENKU8_USERNAME` / `WENKU8_PASSWORD` for ad-hoc testing against the live site. `WENKU8_ENDPOINT` selects the preferred node for the FastAPI service (default `.cc`). `WENKU8_HEADLESS` (truthy default / `0`|`false`|`no`|`off`|`headed` → headed) switches the browser mode when `Wenku8API(headless=...)` is not passed explicitly; headed mode needs an X display. The normal local command explicitly uses `DISPLAY=:0` and `WENKU8_HEADLESS=0`; on a server without a real display, use `xvfb-run -a` instead.

## Architecture

The whole library is one package, `wenku8/`, with `Wenku8API` (in `api.py`) as the sole entry point. The central design tension is **Cloudflare bypass**, implemented as a hybrid transport:

### Two transport paths

1. **httpx primary path** — a persistent `AsyncClient` fetches normal HTML and binary CDN resources. HTML requests may run concurrently, decode raw GBK/Big5 bytes explicitly, retry HTTP 429 once, and try `.cc`/`.net` for read-only requests.
2. **Browser fallback (zendriver / headless Chrome)** — used for login and only when all HTTP node candidates return a solvable CF challenge. Browser navigation remains serialized on the single tab. Full browser cookies are copied into httpx afterward; httpx cookies are copied back before fallback navigation.

> Locking invariant: `_ensure_browser()` must be called *outside* `_nav_lock` to avoid nested-lock deadlock (noted in its docstring).

### Cloudflare handling model (spread across `api.py`)

- `_is_cf_challenge()` / `_is_cf_blocked()` distinguish **challenge** pages (solvable via `tab.verify_cf()`) from **ban/limit** pages (IP throttled, e.g. error 1015 — *not* solvable).
- `_wait_cf()` only invokes `verify_cf()` when a real challenge is detected; it never calls it unconditionally (an earlier unconditional call cost 15s/request — see commit `946c110`). On a ban page it raises `RateLimitException` immediately instead of spinning to timeout.
- `_request_html()` classifies blocked pages, solvable challenges, 429 and ordinary HTTP errors before returning content. `_navigate_browser()` repeats the CF checks after fallback navigation.
- CDN (`_fetch_binary`) detects CF challenge in the response body and raises `CloudflareChallengeException`; HTTP 429 from `dl1` triggers fallback to `dl2` in `get_full_novel_content()`.

### Parsing & error contract

- HTML is parsed with `lxml.etree.HTML(html)`. The transport explicitly decodes raw GBK/Big5 bytes (or receives Unicode DOM from Chromium), so **do not set an encoding in lxml**.
- When an expected XPath node is missing, raise `PageParseError(message, html, xpath=...)` (carries a 2000-char page snippet for debugging) rather than returning empty/`None` or letting `IndexError` propagate. See `get_novel_content`, `_search_page_parser`, `get_bookshelf`. `extract_text`/`separate_chinese_colon` in `utils.py` are the tolerant helpers for fields where absence is tolerable.

### Concurrency, auth, rate-limit decorators

- `@login_required` — checks `is_logged_in` (truthy `_phpsessid`); raises `NotLoggedInException`.
- Normal searches have no fixed cooldown. HTTP 429 honors `Retry-After` (capped at 5 seconds) and retries once.
- Parsed API results use a byte-bounded in-memory LRU (64 MiB by default) with per-kind TTLs and singleflight request coalescing. Full-book TXT and indexes are cached as canonical simplified Chinese, so `get_novel_content_via_full` neither re-downloads the book nor re-navigates to the index for every chapter. Images are deliberately excluded from the server cache.

### Encoding / language conversion

`Lang` (`consts.py`) maps to Wenku8 charsets: `zh_CN`→`gbk`, `zh_TW`→`big5`. All public methods take a `lang` param and run results through `lang_convent()` (`utils.py`), which GB↔Big5-translates strings *and* recursively walks dataclasses/lists. The translation tables (`GB_TO_BIG5`/`BIG5_TO_GB`) are derived from Wenku8's official `GB_BIG5.js` (commit `31bc8c8`) — edit them only from that source.

### Site-specific quirks worth remembering

- Most page URLs take `&charset=gbk`. **`bookcase.php` (bookshelf) must NOT** — appending `charset` makes Cloudflare block it (commit `42dad78`).
- Search with a single hit redirects to a `*.htm` info page; `search_novel` requests `want_url=True` and branches on `final_url.endswith(".htm")`.
- Proxy support: `proxy` arg applies to *both* paths. Chromium's `--proxy-server` doesn't accept `socks5h://`, so `_ensure_browser` rewrites it to `socks5://` (Chromium resolves DNS at the proxy, equivalent to `socks5h`). httpx takes the original `socks5h://` directly.
- List pages have two layouts: `search.php`/`toplist.php` are table-based (`_search_page_parser`), while `articlelist.php` (finished novels) uses 373px div cards (`_card_list_parser`). `tags.php` (by category) returns the table layout when there are results.
- `_strip_tbody` removes all `<tbody>` tags, so parsers migrated from hikari (which relies on `tbody`) must use `table//tr` directly, never `tbody//tr` (e.g. `get_user_info`).
- `reviews.php`/`reviewshow.php` easily trigger Cloudflare challenges when called in rapid succession; space out requests or retry. `bookcase.php?delid=` redirects to the user-center page with no confirmation text, so `remove_from_bookshelf` returns `bool` (not a message string like `add_to_bookshelf`/`vote_novel`).
