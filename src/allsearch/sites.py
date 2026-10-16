"""Registry of well-known sites: how users name them, where they live, what a
content page looks like, and whether a public API can read their threads.

This is general knowledge about the web, not about any benchmark: it lets the
service notice that "V2EX 上…" or "on Hacker News" restricts the answer to one
site, and check that the evidence actually comes from that site's content
pages rather than its home page or a third-party summary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse


@dataclass(frozen=True, slots=True)
class Site:
    key: str
    name: str
    domains: tuple[str, ...]
    aliases: tuple[str, ...]
    language: str = "multi"  # zh | en | multi
    kind: str = "web"  # forum | social | docs | reference | news | video | code | web
    content_path: str = r"/.+"  # regex on the decoded path+query of a content page
    reader: str | None = None  # thread reader that can read posts and comments
    access: str = "public"  # public | login | paywall | anti_bot
    alias_patterns: tuple[re.Pattern[str], ...] = field(default=(), compare=False, repr=False)

    def owns(self, host: str) -> bool:
        host = (host or "").lower().rstrip(".")
        return any(host == d or host.endswith("." + d) for d in self.domains)

    def is_content_url(self, url: str) -> bool:
        parsed = urlparse(url)
        if not self.owns(parsed.hostname or ""):
            return False
        target = unquote(parsed.path or "") + (("?" + unquote(parsed.query)) if parsed.query else "")
        return bool(re.search(self.content_path, target))


def _site(
    key: str,
    name: str,
    domains: tuple[str, ...],
    aliases: tuple[str, ...],
    **kwargs: object,
) -> Site:
    patterns = []
    for alias in aliases:
        if alias.startswith("re:"):
            patterns.append(re.compile(alias[3:], re.I))
        elif re.search(r"[㐀-鿿]", alias):
            patterns.append(re.compile(re.escape(alias), re.I))
        else:
            patterns.append(re.compile(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])", re.I))
    return Site(key=key, name=name, domains=domains, aliases=aliases, alias_patterns=tuple(patterns), **kwargs)  # type: ignore[arg-type]


SITES: tuple[Site, ...] = (
    # Forums and communities
    _site("hackernews", "Hacker News", ("news.ycombinator.com",), ("hacker news", "hackernews", "ycombinator", "re:\\bHN\\b"),
          language="en", kind="forum", content_path=r"item\?id=\d+", reader="hackernews"),
    _site("v2ex", "V2EX", ("v2ex.com", "v2ex.co"), ("v2ex",), language="zh", kind="forum",
          content_path=r"^/t/\d+", reader="v2ex"),
    _site("reddit", "Reddit", ("reddit.com", "redd.it"), ("reddit", "re:(?<![A-Za-z0-9])r/[A-Za-z0-9_]{2,21}\\b"),
          language="en", kind="forum", content_path=r"/comments/[a-z0-9]+", reader="reddit"),
    _site("stackoverflow", "Stack Overflow", ("stackoverflow.com",), ("stack overflow", "stackoverflow"),
          language="en", kind="forum", content_path=r"^/(questions|q|a)/\d+", reader="stackexchange"),
    _site("superuser", "Super User", ("superuser.com",), ("super user", "superuser"), language="en", kind="forum",
          content_path=r"^/(questions|q|a)/\d+", reader="stackexchange"),
    _site("serverfault", "Server Fault", ("serverfault.com",), ("server fault", "serverfault"), language="en",
          kind="forum", content_path=r"^/(questions|q|a)/\d+", reader="stackexchange"),
    _site("askubuntu", "Ask Ubuntu", ("askubuntu.com",), ("ask ubuntu", "askubuntu"), language="en", kind="forum",
          content_path=r"^/(questions|q|a)/\d+", reader="stackexchange"),
    _site("lobsters", "Lobsters", ("lobste.rs",), ("lobsters", "lobste.rs"), language="en", kind="forum",
          content_path=r"^/s/[a-z0-9]+"),
    _site("zhihu", "知乎", ("zhihu.com",), ("知乎", "zhihu"), language="zh", kind="forum",
          content_path=r"^/(question/\d+|p/\d+|answer/\d+)", access="login"),
    _site("tieba", "百度贴吧", ("tieba.baidu.com",), ("贴吧", "tieba"), language="zh", kind="forum",
          content_path=r"^/p/\d+", access="login"),
    _site("chiphell", "Chiphell", ("chiphell.com",), ("chiphell",), language="zh", kind="forum",
          content_path=r"(thread-\d+|viewthread)", access="anti_bot"),
    _site("nga", "NGA", ("nga.cn", "ngabbs.com", "bbs.nga.cn"), ("NGA 论坛", "艾泽拉斯国家地理"), language="zh", kind="forum",
          content_path=r"read\.php\?tid=\d+", access="login"),
    _site("hupu", "虎扑", ("hupu.com",), ("虎扑", "hupu"), language="zh", kind="forum", content_path=r"/\d+\.html"),
    _site("douban", "豆瓣", ("douban.com",), ("豆瓣", "douban"), language="zh", kind="forum",
          content_path=r"/(group/topic|note|review|subject)/\d+"),
    _site("quora", "Quora", ("quora.com",), ("quora",), language="en", kind="forum", content_path=r"/.+", access="login"),
    _site("discourse_python", "discuss.python.org", ("discuss.python.org",), ("discuss.python.org", "python discourse"),
          language="en", kind="forum", content_path=r"^/t/"),
    # Social
    _site("x", "X (Twitter)", ("x.com", "twitter.com"), ("twitter", "推特", "tweet", "推文", "re:(?:\\bon X\\b|\\bX (?:post|status|account|thread)|(?<![A-Za-z])X ?上)"),
          language="multi", kind="social", content_path=r"/status(es)?/\d+", access="login"),
    _site("weibo", "微博", ("weibo.com", "weibo.cn"), ("微博", "weibo"), language="zh", kind="social",
          content_path=r"^/(\d+/[A-Za-z0-9]+|status/\d+|detail/\d+)", access="login"),
    _site("xiaohongshu", "小红书", ("xiaohongshu.com", "xhslink.com"), ("小红书", "xiaohongshu", "rednote"), language="zh",
          kind="social", content_path=r"/(explore|discovery/item)/[0-9a-f]+", access="login"),
    _site("telegram", "Telegram", ("t.me", "telegram.org", "telegram.me"), ("telegram", "电报频道"), language="multi",
          kind="social", content_path=r"^/(s/)?[A-Za-z0-9_]{4,}"),
    _site("mastodon", "Mastodon", ("mastodon.social",), ("mastodon",), language="multi", kind="social",
          content_path=r"/@[^/]+/\d+"),
    _site("wechat", "微信公众号", ("mp.weixin.qq.com",), ("公众号", "微信文章", "weixin", "wechat"), language="zh",
          kind="social", content_path=r"^/s[/?]", access="login"),
    # Video
    _site("bilibili", "哔哩哔哩", ("bilibili.com", "b23.tv"), ("bilibili", "哔哩哔哩", "b站", "re:\\bB ?站"), language="zh",
          kind="video", content_path=r"/(video/(BV|av)[0-9A-Za-z]+|read/cv\d+)"),
    _site("youtube", "YouTube", ("youtube.com", "youtu.be"), ("youtube", "油管"), language="multi", kind="video",
          content_path=r"(watch\?v=|/shorts/|^/[A-Za-z0-9_-]{11}$)"),
    # Code
    _site("github", "GitHub", ("github.com",), ("github",), language="en", kind="code",
          content_path=r"^/[^/]+/[^/]+(/(issues|pull|discussions|releases|blob|tree|commit|wiki)/.+)?$"),
    _site("gitlab", "GitLab", ("gitlab.com",), ("gitlab",), language="en", kind="code", content_path=r"^/[^/]+/[^/]+"),
    _site("pypi", "PyPI", ("pypi.org",), ("pypi",), language="en", kind="code", content_path=r"^/project/"),
    _site("npm", "npm", ("npmjs.com",), ("npmjs", "re:\\bnpm (?:package|registry)\\b"), language="en", kind="code",
          content_path=r"^/package/"),
    # Reference and docs
    _site("wikipedia", "Wikipedia", ("wikipedia.org",), ("wikipedia", "维基百科", "維基百科", "wiki 百科"), language="multi",
          kind="reference", content_path=r"^/(wiki|zh-[a-z]+)/.+"),
    _site("baike", "百度百科", ("baike.baidu.com",), ("百度百科",), language="zh", kind="reference", content_path=r"^/item/"),
    _site("mdn", "MDN", ("developer.mozilla.org",), ("mdn",), language="en", kind="docs", content_path=r"/docs/"),
    _site("python_docs", "Python docs", ("docs.python.org",), ("python docs", "python documentation", "python 官方文档"),
          language="en", kind="docs", content_path=r"/.+\.html"),
    _site("rfc", "RFC Editor", ("rfc-editor.org", "datatracker.ietf.org"), ("rfc editor", "re:\\bRFC ?\\d{3,5}\\b"),
          language="en", kind="docs", content_path=r"(rfc\d+|/doc/)"),
    _site("arxiv", "arXiv", ("arxiv.org",), ("arxiv",), language="en", kind="reference", content_path=r"/(abs|pdf)/"),
    _site("lwn", "LWN.net", ("lwn.net",), ("lwn",), language="en", kind="news", content_path=r"^/Articles/\d+"),
    _site("medium", "Medium", ("medium.com",), ("medium.com",), language="en", kind="web", content_path=r"/.+-[0-9a-f]{8,}"),
    _site("devto", "DEV", ("dev.to",), ("dev.to",), language="en", kind="web", content_path=r"^/[^/]+/.+"),
    _site("csdn", "CSDN", ("csdn.net",), ("csdn",), language="zh", kind="web", content_path=r"/article/details/\d+"),
    _site("juejin", "掘金", ("juejin.cn",), ("掘金", "juejin"), language="zh", kind="web", content_path=r"^/post/\d+"),
    # News
    _site("wsj", "The Wall Street Journal", ("wsj.com",), ("wall street journal", "华尔街日报", "re:\\bWSJ\\b"),
          language="en", kind="news", content_path=r"^/(articles|[a-z-]+/[a-z0-9-]+)", access="paywall"),
    _site("nyt", "The New York Times", ("nytimes.com",), ("new york times", "纽约时报", "re:\\bNYT\\b"), language="en",
          kind="news", content_path=r"/\d{4}/\d{2}/", access="paywall"),
    _site("reuters", "Reuters", ("reuters.com",), ("reuters", "路透"), language="en", kind="news", content_path=r"/.+-\d{4}-\d{2}-\d{2}"),
    _site("bbc", "BBC", ("bbc.com", "bbc.co.uk"), ("re:\\bBBC\\b",), language="multi", kind="news", content_path=r"/news/"),
)

SITE_BY_KEY = {site.key: site for site in SITES}


def site_for_url(url: str) -> Site | None:
    host = (urlparse(url).hostname or "").lower()
    for site in SITES:
        if site.owns(host):
            return site
    return None


_PLACE_AFTER_ZH = re.compile(
    r"^\s*.{0,7}?(?:上(?![传线网班市升面午海手架课映涨载述限])|里|中(?![文国间心])|的?(?:帖子|主题帖?|讨论帖?|文章|视频|笔记|词条|回答|页面|条目|评论|微博|频道|账号|原文|全文))",
    re.S,
)
# At most one word between the site name and the noun: "Wikipedia HTTP article",
# "Hacker News thread", but not "Reddit API to fetch comments".
_PLACE_AFTER_EN = re.compile(
    r"^(?:\.\w+)?(?:\s+[\w.+#-]+)?\s+(?:threads?|posts?|pages?|articles?|questions?|discussions?|videos?|"
    r"releases?|entry|entries|status|issues?|answers?|submissions?|channels?|accounts?|repo(?:sitory)?|"
    r"items?|topics?|comments?|text|web preview|viewthread)\b",
    re.I,
)
_PLACE_BEFORE = re.compile(r"(?:\b(?:on|in|from|at|via|of)\s+(?:the\s+)?|在|去|到|找|搜)\s*$", re.I)


def used_as_place(text: str, site: Site) -> bool:
    """True when the text treats the site as where the answer lives
    ("V2EX 上…", "on Hacker News", "GitHub release page"), not as a topic
    ("GitHub Actions caching error", "the Reddit API rate limit")."""
    spans: list[tuple[int, int]] = []
    for pattern in site.alias_patterns:
        spans += [(m.start(), m.end()) for m in pattern.finditer(text)]
    for domain in site.domains:
        spans += [(m.start(), m.end()) for m in re.finditer(re.escape(domain), text, re.I)]
    for start, end in spans:
        if site.key == "x":
            return True  # the X alias patterns already require "X post", "on X", "X 上"
        after = text[end : end + 48]
        if _PLACE_AFTER_ZH.search(after) or _PLACE_AFTER_EN.search(after) or _PLACE_BEFORE.search(text[max(0, start - 12) : start]):
            return True
        if re.match(r"\s*(?:\.\w+)?\s*/", text[end:]):
            return True  # "r/LocalLLaMA", "github.com/owner/repo"
    return False


def mentioned_sites(text: str) -> list[tuple[int, Site]]:
    """Sites named in ``text``, with the position of the first mention."""
    found: dict[str, tuple[int, Site]] = {}
    for site in SITES:
        for pattern in site.alias_patterns:
            match = pattern.search(text)
            if match and (site.key not in found or match.start() < found[site.key][0]):
                found[site.key] = (match.start(), site)
        for domain in site.domains:
            match = re.search(r"(?<![A-Za-z0-9.-])" + re.escape(domain) + r"(?![A-Za-z0-9-])", text, re.I)
            if match and (site.key not in found or match.start() < found[site.key][0]):
                found[site.key] = (match.start(), site)
    return sorted(found.values(), key=lambda item: item[0])


def strip_site_names(text: str) -> str:
    """Text with site names removed, so "Hacker News" is not a news signal."""
    out = text
    for _, site in mentioned_sites(text):
        for pattern in site.alias_patterns:
            out = pattern.sub(" ", out)
        for domain in site.domains:
            out = re.sub(re.escape(domain), " ", out, flags=re.I)
    return out
