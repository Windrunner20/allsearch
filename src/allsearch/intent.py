"""What a query asks for, beyond its topic.

``analyze`` turns a query into explicit, checkable requirements: which sites
the evidence must come from, whether comments or a specific thread are wanted,
whether every named source must be covered, the language the sources should
be in, a recency window, and the sides of a comparison. Coverage checking
(coverage.py) evaluates the evidence against these requirements, and the
orchestrator only spends extra searches on requirements that are not met.

Everything here is a pure function of the query text and the request
parameters; nothing depends on any benchmark case.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from allsearch.sites import SITE_BY_KEY, Site, mentioned_sites, site_for_url, used_as_place

_CJK = re.compile(r"[㐀-鿿豈-﫿]")
_LATIN = re.compile(r"[A-Za-z]")

COMMENT_RE = re.compile(
    r"评论|回复|回帖|跟帖|楼层|楼中楼|网友(?:说|认为|提到)|高赞回答|回答正文|答主"
    r"|\b(?:comments?|commenters?|repl(?:y|ies)|replied|answers?|responses?|upvoted)\b",
    re.I,
)
THREAD_RE = re.compile(
    r"帖子|主题帖|讨论帖|帖|讨论|话题|问答|问题页|笔记|单条|原帖"
    r"|\b(?:thread|discussion|post|topic|question page|submission|status)\b",
    re.I,
)
OFFICIAL_RE = re.compile(r"官方|官网|\b(?:official|primary source)\b", re.I)
ORIGINAL_RE = re.compile(r"原文|原始|全文|出处|\b(?:original|full text|verbatim)\b", re.I)
ALL_SITES_RE = re.compile(r"分别|各找|各一|都要|同时|两边|\b(?:both|each|respectively)\b", re.I)
ANY_SITE_RE = re.compile(r"或者|或|\bor\b", re.I)
WANT_EN_RE = re.compile(r"英文|英语|\bin English\b|English[- ]language", re.I)
WANT_ZH_RE = re.compile(r"中文(?:资料|帖子|文章|原文|内容|页面|网站)|\bin Chinese\b|Chinese[- ]language", re.I)
ASKED_IN_ZH_RE = re.compile(r"用中文找|用中文搜")

_RECENCY = [
    (re.compile(r"(?:过去|最近|近)\s*(\d{1,3})\s*(?:个)?\s*小时"), 1),
    (re.compile(r"(?:过去|最近|近)\s*(\d{1,3})\s*天"), 24),
    (re.compile(r"(?:过去|最近|近)\s*(\d{1,2})\s*(?:周|个星期|星期)"), 168),
    (re.compile(r"\b(?:past|last)\s+(\d{1,3})\s+hours?\b", re.I), 1),
    (re.compile(r"\b(?:past|last)\s+(\d{1,3})\s+days?\b", re.I), 24),
    (re.compile(r"\b(?:past|last)\s+(\d{1,2})\s+weeks?\b", re.I), 168),
]
_RECENCY_WORDS = [
    (re.compile(r"今天|今日|\btoday\b|\bpast day\b|\blast 24h\b", re.I), 24),
    (re.compile(r"昨天|\byesterday\b", re.I), 48),
    (re.compile(r"本周|这周|这一周|\bthis week\b|\bpast week\b|\blast week\b", re.I), 168),
    (re.compile(r"本月|这个月|\bthis month\b|\bpast month\b", re.I), 744),
]

_COMPARE_SPLIT = [
    re.compile(r"(?:对比|比较)\s*(?P<a>.+?)\s*(?:和|与|跟|及|以及|vs\.?|VS)\s*(?P<b>.+)$"),
    re.compile(r"\bcompare\s+(?P<a>.+?)\s+(?:with|and|to|against|vs\.?)\s+(?P<b>.+)$", re.I),
    re.compile(r"^(?P<a>.+?)\s+(?:vs\.?|versus)\s+(?P<b>.+)$", re.I),
]

_EN_STOP = set(
    """a an the of in on at to for from by with about into over and or but is are was were be been
    this that these those which who whom what where when how why find search show give list read quote
    get open locate page pages article articles post posts thread threads discussion discussions topic
    topics site sites one some any all both each reader readers user users people says say said saying
    mention mentions mentioned public official text entry document documentation doc docs""".split()
)
_ZH_META = re.compile(
    r"用中文找|用中文搜|找一篇|找一条|找一个|找到|找|搜索|请|上关于|上讨论|上介绍|上比较|上的|关于|里讨论|里的|里|"
    r"具体的|具体|主题帖|讨论帖|帖子|的帖子|原文链接|原文|链接|一条|一篇|笔记|英文|中文|官方|页面|内容都要提到|都要提到|分别"
)


@dataclass(slots=True)
class SiteRequirement:
    site: Site
    domains: tuple[str, ...]
    content_page: bool  # the user wants a specific post/page, not the site home

    def to_dict(self) -> dict[str, Any]:
        return {"site": self.site.key, "name": self.site.name, "domains": list(self.domains), "content_page": self.content_page}


@dataclass(slots=True)
class QueryIntent:
    query: str
    language: str
    sites: list[SiteRequirement] = field(default_factory=list)
    all_sites: bool = False
    comments: bool = False
    thread: bool = False
    official: bool = False
    original: bool = False
    target_language: str | None = None
    # True when the user asked for the language in words ("英文原文", "in English"),
    # not merely implied by naming a site that is written in that language.
    target_language_explicit: bool = False
    recency_hours: int | None = None
    entities: list[str] = field(default_factory=list)
    topic: str = ""
    comment_focus: str = ""
    # Sites named only as a topic ("GitHub Actions", "the Reddit API"): no requirement.
    mentioned: list[str] = field(default_factory=list)

    @property
    def site_domains(self) -> list[str]:
        out: list[str] = []
        for req in self.sites:
            for domain in req.domains:
                if domain not in out:
                    out.append(domain)
        return out

    @property
    def needs_rewrite(self) -> bool:
        """A CJK query aimed at English sources needs English keywords."""
        if self.language != "zh":
            return False
        if self.target_language == "en":
            return True
        return any(req.site.language == "en" for req in self.sites)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"language": self.language}
        if self.sites:
            out["sites"] = [req.to_dict() for req in self.sites]
            out["all_sites"] = self.all_sites
        for key in ("comments", "thread", "official", "original"):
            if getattr(self, key):
                out[key] = True
        if self.target_language:
            out["target_language"] = self.target_language
        if self.recency_hours:
            out["recency_hours"] = self.recency_hours
        if self.entities:
            out["entities"] = self.entities
        if self.topic:
            out["topic"] = self.topic
        if self.comment_focus:
            out["comment_focus"] = self.comment_focus
        return out


def language_of(text: str) -> str:
    cjk = len(_CJK.findall(text))
    latin = len(_LATIN.findall(text))
    return "zh" if cjk and cjk * 2 >= latin * 0.5 else "en"


def _recency_hours(query: str) -> int | None:
    for pattern, unit in _RECENCY:
        match = pattern.search(query)
        if match:
            return max(1, int(match.group(1)) * unit)
    for pattern, hours in _RECENCY_WORDS:
        if pattern.search(query):
            return hours
    return None


def _site_domains(site: Site, query: str) -> tuple[str, ...]:
    if site.key == "wikipedia":
        if re.search(r"中文|Chinese", query, re.I):
            return ("zh.wikipedia.org",)
        if re.search(r"English|英文", query, re.I):
            return ("en.wikipedia.org",)
    return site.domains


def _clean_side(text: str) -> str:
    text = re.sub(r"[，,。;；:：].*$", "", text)
    text = re.sub(r"(?:的)?(?:帖子|讨论|文档|页面|章节|手感)+$", "", text.strip())
    return keywords(text, drop_sites=True)


def _entities(query: str) -> list[str]:
    for pattern in _COMPARE_SPLIT:
        match = pattern.search(query)
        if match:
            sides = [_clean_side(match.group("a")), _clean_side(match.group("b"))]
            if all(len(side) >= 2 for side in sides):
                return sides
    return []


def keywords(text: str, *, drop_sites: bool = True) -> str:
    """Rule-based keyword query: site names and request words removed."""
    out = text
    if drop_sites:
        for _, site in mentioned_sites(text):
            if not used_as_place(text, site):
                continue  # "GitHub Actions" keeps "GitHub" in the topic
            # Domains first, so "LWN.net" does not leave ".net" behind; a trailing
            # 上/里/中 belongs to the site mention ("V2EX 上", "贴吧里").
            for domain in site.domains:
                out = re.sub(re.escape(domain) + r"\s*[上里中]?", " ", out, flags=re.I)
            for pattern in site.alias_patterns:
                out = re.sub(pattern.pattern + r"(?:\.(?:net|com|org))?\s*[上里中]?", " ", out, flags=pattern.flags)
    out = _ZH_META.sub(" ", out)
    out = re.sub(r"(?<![A-Za-z0-9])r/", " ", out)
    words = []
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+#_'-]*|[㐀-鿿]+", out):
        if token.lower() in _EN_STOP:
            continue
        if _CJK.match(token):
            token = _ZH_EDGE.sub("", token)
            if len(token) < 2:
                continue
        words.append(token)
    return " ".join(words).strip()


_ZH_EDGE = re.compile(r"^(?:上|把|和|与|在|对|将)+|(?:的|了|吗|呢|是谁|来讨论|讨论)+$")


_COMMENT_SPLIT = re.compile(
    r"(?:,|，|;|；)?\s*(?:and\s+)?(?:find|read|quote|show|get)\s+(?:(?:a|one|the|both|two|any)\s+){0,2}(?:reader\s+)?"
    r"(?:comments?|repl(?:y|ies))\b(?:\s+(?:that|which|who|about|saying|says|mentioning|on))?\s*"
    r"|(?:找|读|引用)(?:出|到)?(?:一条|两条|一个)?(?:评论|回复|回帖)(?:说|提到|讲|里)?|并给出一条回复原文|给出一条回复",
    re.I,
)


def _split_comment_focus(query: str) -> tuple[str, str]:
    match = _COMMENT_SPLIT.search(query)
    if not match:
        return query, ""
    before, after = query[: match.start()], query[match.end() :]
    return before.strip(" ,，"), after.strip(" ,，.。")


def analyze(query: str, *, include_domains: list[str] | None = None) -> QueryIntent:
    language = language_of(query)
    intent = QueryIntent(query=query, language=language)
    if not include_domains:
        for _, site in mentioned_sites(query):
            if used_as_place(query, site):
                intent.sites.append(
                    SiteRequirement(site=site, domains=_site_domains(site, query), content_page=True)
                )
            else:
                intent.mentioned.append(site.key)
    else:
        # A domain filter on a known site is the same requirement as naming the
        # site, so site-native search and thread reading still apply.
        for raw in include_domains:
            domain = raw.strip().lower().lstrip(".")
            site = site_for_url(f"https://{domain}/")
            if site is not None and not any(r.site.key == site.key for r in intent.sites):
                intent.sites.append(SiteRequirement(site=site, domains=(domain,), content_page=True))
    intent.comments = bool(COMMENT_RE.search(query))
    intent.thread = bool(THREAD_RE.search(query)) or intent.comments
    intent.official = bool(OFFICIAL_RE.search(query))
    intent.original = bool(ORIGINAL_RE.search(query))
    if len(intent.sites) >= 2:
        intent.all_sites = bool(ALL_SITES_RE.search(query)) or not ANY_SITE_RE.search(query)

    if WANT_EN_RE.search(query) and (language == "zh" or ASKED_IN_ZH_RE.search(query)):
        intent.target_language = "en"
        intent.target_language_explicit = True
    elif WANT_ZH_RE.search(query) and language == "en":
        intent.target_language = "zh"
        intent.target_language_explicit = True
    elif intent.sites and all(req.site.language == "en" for req in intent.sites) and language == "zh":
        intent.target_language = "en"
    elif intent.sites and all(req.site.language == "zh" for req in intent.sites) and language == "en":
        intent.target_language = "zh"

    intent.recency_hours = _recency_hours(query)
    intent.entities = _entities(query)
    story, focus = _split_comment_focus(query) if intent.comments else (query, "")
    intent.topic = keywords(story)
    intent.comment_focus = keywords(focus, drop_sites=False) if focus else ""
    return intent


def site(key: str) -> Site:
    return SITE_BY_KEY[key]
