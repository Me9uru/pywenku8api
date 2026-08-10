class NotLoggedInException(Exception):
    pass


class RateLimitException(Exception):
    pass


class CloudflareChallengeException(Exception):
    """HTTP 请求被 Cloudflare 质询页拦截。

    HTML 主路径会捕获此异常并用浏览器兜底；CDN 二进制路径无法可靠地把
    浏览器质询结果转换成原始资源响应，因此将异常交给调用方。
    """


class PageParseError(Exception):
    """页面解析失败：期望的节点/结构在页面中缺失。

    常见于页面返回 CF 质询残留页、等待页、404 或 wenku8 结构变化。
    异常消息携带页面 HTML 片段以供调试。
    """

    def __init__(self, message: str, html: str = "", *, xpath: str = ""):
        self.html = html
        self.xpath = xpath
        detail = html[:2000] if html else "(无页面内容)"
        super().__init__(f"{message} [xpath={xpath or 'N/A'}] 页面片段: {detail}")


class InvalidUrlError(Exception):
    """传入的 URL 非法或不在允许域内（如 get_picture 的 SSRF 防护白名单）。"""
    pass
