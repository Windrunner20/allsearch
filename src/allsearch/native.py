"""Site-native discovery and thread readers that use public, documented APIs.

* Hacker News: Algolia HN Search API (search and full comment trees).
* V2EX: the official API v1 (topic and replies) and the public sov2ex search
  index of V2EX topics.
* Stack Exchange: API 2.3 (search, question body, answers). An app key raises
  the anonymous 300 requests/day per IP quota.
* Wikipedia: the MediaWiki REST search endpoint.
* Reddit: only with an OAuth app (client credentials); anonymous JSON returns
  HTTP 403 and is not worked around.

Nothing here logs in as a user, solves captchas, or scrapes past a wall. A
thread that cannot be read is reported as ``failed`` or ``blocked`` with the
reason, so callers can tell "not found" from "found but not readable".

Search results from these sources carry real titles and snippets, and thread
reads carry comment ids, authors, timestamps and parent ids, so a comment in
the evidence is always something the platform actually served.
"""

from __future__ import annotations

import base64
import html
import math
import re
import time
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

from allsearch import __version__
from allsearch.cache import MemoryCache
from allsearch.errors import AllSearchError, AuthError, TransportError, to_public_error
from allsearch.health import HealthRegistry
from allsearch.models import Comment, ProviderResultItem, ThreadEvidence
from allsearch.transport import HttpTransport

USER_AGENT = f"AllSearch/{__version__} (+https://github.com/Windrunner20/allsearch)"
_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}
MAX_COMMENTS_STORED = 400

STACKEXCHANGE_SITES = {
    "stackoverflow.com": "stackoverflow",
    "superuser.com": "superuser",
    "serverfault.com": "serverfault",
    "askubuntu.com": "askubuntu",
    "math.stackexchange.com": "math",
    "unix.stackexchange.com": "unix",
}


# -- text helpers -------------------------------------------------------------

_TAG_BREAK = re.compile(r"<\s*(?:br|/p|/div|/li|/h[1-6]|p)\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_LINK = re.compile(r"<a\s[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.I | re.S)


def html_to_text(raw: str | None) -> str:
    if not raw:
        return ""
    text = _LINK.sub(lambda m: m.group(2) if m.group(2).strip() else m.group(1), raw)
    text = _TAG_BREAK.sub("\n", text)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _iso(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?|[㐀-鿿]")
_STOP = set(
    "a an the of in on at to for from by with and or but is are was were be been it its this that these those "
    "which who what when how why as not no do does did can could should would will just so if then than there "
    "their they them he she we you i my your our me us about into over also very more most some any".split()
)


def _stem(token: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def terms(text: str) -> list[str]:
    tokens = [t for t in _WORD.findall((text or "").lower()) if t not in _STOP]
    out: list[str] = []
    for token in tokens:
        if len(token) == 1 and not ("㐀" <= token <= "鿿"):
            continue
        out.append(_stem(token))
    # CJK: score on bigrams, which carry most of the meaning of short words.
    cjk = [t for t in tokens if len(t) == 1 and "㐀" <= t <= "鿿"]
    out += [a + b for a, b in zip(cjk, cjk[1:])]
    return out


def rank_comments(comments: list[Comment], focus: str, *, limit: int) -> list[Comment]:
    """BM25 over comment texts. Without a focus, keep thread order."""
    query = set(terms(focus))
    if not query or not comments:
        return comments[:limit]
    docs = [terms(c.text) for c in comments]
    avg = sum(len(d) for d in docs) / max(1, len(docs)) or 1.0
    df: Counter[str] = Counter()
    for doc in docs:
        df.update(set(doc) & query)
    n = len(docs)
    scored: list[tuple[float, int, Comment]] = []
    for index, (comment, doc) in enumerate(zip(comments, docs)):
        tf = Counter(doc)
        score = 0.0
        for term in query:
            if not tf.get(term):
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            freq = tf[term]
            score += idf * freq * 2.2 / (freq + 1.2 * (0.25 + 0.75 * len(doc) / avg))
        if score > 0:
            scored.append((score, -index, comment))
    scored.sort(reverse=True)
    out = []
    for score, _, comment in scored[:limit]:
        out.append(comment.model_copy(update={"relevance": round(score, 3)}))
    return out


# -- URL parsing ----------------------------------------------------------------


def thread_ref(url: str) -> tuple[str, str] | None:
    """(platform, thread id) for a URL a reader understands."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "news.ycombinator.com":
        ids = parse_qs(parsed.query).get("id") or []
        if ids and ids[0].isdigit():
            return "hackernews", ids[0]
        return None
    if host.endswith("v2ex.com") or host.endswith("v2ex.co"):
        match = re.match(r"^/t/(\d+)", parsed.path)
        return ("v2ex", match.group(1)) if match else None
    site = STACKEXCHANGE_SITES.get(host[4:] if host.startswith("www.") else host)
    if site:
        match = re.match(r"^/(?:questions|q)/(\d+)", parsed.path)
        return ("stackexchange", f"{site}:{match.group(1)}") if match else None
    if host == "redd.it" or host.endswith("reddit.com"):
        match = re.search(r"/comments/([a-z0-9]+)", parsed.path)
        if match:
            return "reddit", match.group(1)
        if host == "redd.it" and re.fullmatch(r"/[a-z0-9]+", parsed.path):
            return "reddit", parsed.path[1:]
    return None


def canonical_thread_url(url: str) -> str:
    """Official URL for a thread on a mirror host (hk.v2ex.com, cn.v2ex.com, …)."""
    ref = thread_ref(url)
    if ref is None:
        return url
    platform, thread_id = ref
    if platform == "hackernews":
        return f"https://news.ycombinator.com/item?id={thread_id}"
    if platform == "v2ex":
        return f"https://www.v2ex.com/t/{thread_id}"
    return url


# -- client ---------------------------------------------------------------------


class NativeSources:
    """All site-native calls go through one bounded transport, cache and circuit registry."""

    def __init__(
        self,
        transport: HttpTransport,
        health: HealthRegistry,
        cache: MemoryCache,
        *,
        enabled: bool = True,
        reddit_client_id: str = "",
        reddit_client_secret: str = "",
        stackexchange_key: str = "",
    ) -> None:
        self.transport = transport
        self.health = health
        self.cache = cache
        self.enabled = enabled
        self.reddit_client_id = reddit_client_id
        self.reddit_client_secret = reddit_client_secret
        self.stackexchange_key = stackexchange_key
        self._reddit_token: tuple[str, float] | None = None
        for name in ("hackernews", "v2ex", "stackexchange", "wikipedia"):
            health.register(name, enabled)
        health.register("reddit", enabled and self.reddit_configured)
        self.calls: Counter[str] = Counter()

    @property
    def reddit_configured(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret)

    def can_search(self, site_key: str) -> bool:
        if not self.enabled:
            return False
        if site_key == "reddit":
            return self.reddit_configured and self.health.is_available("reddit")
        source = {"stackoverflow": "stackexchange", "superuser": "stackexchange", "serverfault": "stackexchange",
                  "askubuntu": "stackexchange"}.get(site_key, site_key)
        return source in {"hackernews", "v2ex", "stackexchange", "wikipedia"} and self.health.is_available(source)

    def can_read(self, platform: str) -> bool:
        if not self.enabled:
            return False
        if platform == "reddit":
            return self.reddit_configured and self.health.is_available("reddit")
        return platform in {"hackernews", "v2ex", "stackexchange"} and self.health.is_available(platform)

    async def _get(self, source: str, url: str, *, deadline: float | None, headers: dict[str, str] | None = None) -> Any:
        key = self.cache.make_key("native", {"url": url})
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        self.health.ensure_closed_or_raise(source)
        started = time.perf_counter()
        self.calls[source] += 1
        try:
            data = await self.transport.request_json(
                "GET", url, headers={**_HEADERS, **(headers or {})}, deadline_seconds=deadline, provider=source, allow_list=True
            )
        except AuthError:
            self.health.record_failure(source, "auth_error", retryable=False)
            raise
        except AllSearchError as exc:
            self.health.record_failure(source, exc.code, retryable=exc.retryable)
            raise
        self.health.record_success(source, int((time.perf_counter() - started) * 1000))
        self.cache.set(key, data, 600)
        return data

    # -- discovery ----------------------------------------------------------

    async def search(self, site_key: str, query: str, *, limit: int, deadline: float | None, lang: str = "en") -> list[ProviderResultItem]:
        if site_key == "hackernews":
            return await self._hn_search(query, limit, deadline)
        if site_key == "v2ex":
            return await self._v2ex_search(query, limit, deadline)
        if site_key in {"stackoverflow", "superuser", "serverfault", "askubuntu"}:
            site = {"stackoverflow": "stackoverflow", "superuser": "superuser", "serverfault": "serverfault",
                    "askubuntu": "askubuntu"}[site_key]
            return await self._se_search(site, query, limit, deadline)
        if site_key == "wikipedia":
            return await self._wiki_search(lang, query, limit, deadline)
        if site_key == "reddit" and self.reddit_configured:
            return await self._reddit_search(query, limit, deadline)
        return []

    async def _hn_search(self, query: str, limit: int, deadline: float | None) -> list[ProviderResultItem]:
        url = "https://hn.algolia.com/api/v1/search?" + urlencode({"query": query, "tags": "story", "hitsPerPage": limit})
        data = await self._get("hackernews", url, deadline=deadline)
        out = []
        for hit in (data or {}).get("hits") or []:
            if not isinstance(hit, dict) or not hit.get("objectID"):
                continue
            story_url = hit.get("url") or ""
            snippet = f"{hit.get('points') or 0} points, {hit.get('num_comments') or 0} comments, by {hit.get('author')} on {str(hit.get('created_at') or '')[:10]}"
            if story_url:
                snippet += f". Links to {story_url}"
            out.append(
                ProviderResultItem(
                    title=str(hit.get("title") or ""),
                    url=f"https://news.ycombinator.com/item?id={hit['objectID']}",
                    snippet=snippet,
                    published_at=hit.get("created_at"),
                    source_type="social",
                    provider="hackernews",
                    metadata={"points": hit.get("points"), "num_comments": hit.get("num_comments"), "story_url": story_url},
                )
            )
        return out

    async def _v2ex_search(self, query: str, limit: int, deadline: float | None) -> list[ProviderResultItem]:
        url = "https://www.sov2ex.com/api/search?" + urlencode({"q": query, "size": limit})
        data = await self._get("v2ex", url, deadline=deadline)
        out = []
        for hit in (data or {}).get("hits") or []:
            source = hit.get("_source") if isinstance(hit, dict) else None
            if not isinstance(source, dict) or not source.get("id"):
                continue
            content = str(source.get("content") or "")
            out.append(
                ProviderResultItem(
                    title=str(source.get("title") or ""),
                    url=f"https://www.v2ex.com/t/{source['id']}",
                    snippet=(content[:300] + ("…" if len(content) > 300 else "")),
                    published_at=_iso(source.get("created")),
                    source_type="social",
                    provider="v2ex",
                    metadata={"replies": source.get("replies"), "member": source.get("member"), "index": "sov2ex"},
                )
            )
        return out

    def _se_params(self, **params: Any) -> str:
        if self.stackexchange_key:
            params["key"] = self.stackexchange_key
        return urlencode(params)

    async def _se_search(self, site: str, query: str, limit: int, deadline: float | None) -> list[ProviderResultItem]:
        url = "https://api.stackexchange.com/2.3/search/advanced?" + self._se_params(
            q=query, site=site, order="desc", sort="relevance", pagesize=limit
        )
        data = await self._get("stackexchange", url, deadline=deadline)
        out = []
        for item in (data or {}).get("items") or []:
            if not isinstance(item, dict) or not item.get("link"):
                continue
            out.append(
                ProviderResultItem(
                    title=html.unescape(str(item.get("title") or "")),
                    url=str(item["link"]),
                    snippet=f"score {item.get('score')}, {item.get('answer_count')} answers, tags: {', '.join(item.get('tags') or [])}",
                    published_at=_iso(item.get("creation_date")),
                    source_type="social",
                    provider="stackexchange",
                    metadata={"question_id": item.get("question_id"), "is_answered": item.get("is_answered")},
                )
            )
        return out

    async def _wiki_search(self, lang: str, query: str, limit: int, deadline: float | None) -> list[ProviderResultItem]:
        lang = lang if re.fullmatch(r"[a-z]{2,3}", lang or "") else "en"
        url = f"https://{lang}.wikipedia.org/w/rest.php/v1/search/page?" + urlencode({"q": query, "limit": limit})
        data = await self._get("wikipedia", url, deadline=deadline)
        out = []
        for page in (data or {}).get("pages") or []:
            if not isinstance(page, dict) or not page.get("key"):
                continue
            out.append(
                ProviderResultItem(
                    title=str(page.get("title") or ""),
                    url=f"https://{lang}.wikipedia.org/wiki/{quote(str(page['key']))}",
                    snippet=html_to_text(str(page.get("excerpt") or "")) + (f" — {page['description']}" if page.get("description") else ""),
                    source_type="document",
                    provider="wikipedia",
                )
            )
        return out

    async def _reddit_bearer(self, deadline: float | None) -> str:
        if self._reddit_token and self._reddit_token[1] > time.time() + 60:
            return self._reddit_token[0]
        basic = base64.b64encode(f"{self.reddit_client_id}:{self.reddit_client_secret}".encode()).decode()
        data = await self.transport.request_form(
            "https://www.reddit.com/api/v1/access_token",
            {"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {basic}", "User-Agent": USER_AGENT},
            deadline_seconds=deadline,
            provider="reddit",
        )
        token = str(data.get("access_token") or "")
        if not token:
            raise AuthError("reddit token response had no access_token", provider="reddit")
        self._reddit_token = (token, time.time() + float(data.get("expires_in") or 3600))
        return token

    async def _reddit_search(self, query: str, limit: int, deadline: float | None) -> list[ProviderResultItem]:
        token = await self._reddit_bearer(deadline)
        sub = re.search(r"(?<![A-Za-z0-9])r/([A-Za-z0-9_]{2,21})", query)
        base = f"https://oauth.reddit.com/r/{sub.group(1)}/search" if sub else "https://oauth.reddit.com/search"
        params = {"q": re.sub(r"(?<![A-Za-z0-9])r/[A-Za-z0-9_]+", " ", query).strip(), "limit": limit, "type": "link", "sort": "relevance"}
        if sub:
            params["restrict_sr"] = "1"
        data = await self._get("reddit", base + "?" + urlencode(params), deadline=deadline, headers={"Authorization": f"Bearer {token}"})
        out = []
        for child in ((data or {}).get("data") or {}).get("children") or []:
            post = child.get("data") if isinstance(child, dict) else None
            if not isinstance(post, dict) or not post.get("permalink"):
                continue
            out.append(
                ProviderResultItem(
                    title=str(post.get("title") or ""),
                    url="https://www.reddit.com" + str(post["permalink"]),
                    snippet=str(post.get("selftext") or "")[:300],
                    published_at=_iso(post.get("created_utc")),
                    source_type="social",
                    provider="reddit",
                    metadata={"subreddit": post.get("subreddit"), "num_comments": post.get("num_comments")},
                )
            )
        return out

    # -- thread reading -----------------------------------------------------

    async def read_thread(self, url: str, *, focus: str, deadline: float | None, keep: int = 8) -> ThreadEvidence:
        """Read a thread and its comments. ``comments`` holds up to ``keep``
        comments ranked against ``focus`` first, then the remaining comments in
        thread order (up to MAX_COMMENTS_STORED), so read_result can page all of it."""
        ref = thread_ref(url)
        if ref is None:
            return ThreadEvidence(platform="unknown", url=url, status="failed", error="unsupported_url")
        platform, thread_id = ref
        if not self.can_read(platform):
            reason = "reddit_oauth_not_configured" if platform == "reddit" and not self.reddit_configured else "reader_unavailable"
            return ThreadEvidence(platform=platform, url=url, thread_id=thread_id, status="found", error=reason)
        try:
            if platform == "hackernews":
                thread = await self._hn_read(thread_id, deadline)
            elif platform == "v2ex":
                thread = await self._v2ex_read(thread_id, deadline)
            elif platform == "stackexchange":
                thread = await self._se_read(thread_id, deadline)
            else:
                thread = await self._reddit_read(thread_id, deadline)
        except AllSearchError as exc:
            pub = to_public_error(exc, provider=platform)
            status = "blocked" if isinstance(exc, AuthError) or getattr(exc, "status_code", None) == 403 else "failed"
            return ThreadEvidence(platform=platform, url=url, thread_id=thread_id, status=status, error=pub.code)
        ranked = rank_comments(thread.comments, focus, limit=keep) if focus else []
        seen = {c.id for c in ranked}
        ordered = ranked + [c for c in thread.comments if c.id not in seen]
        thread.comments = ordered[:MAX_COMMENTS_STORED]
        return thread

    async def _hn_read(self, item_id: str, deadline: float | None) -> ThreadEvidence:
        data = await self._get("hackernews", f"https://hn.algolia.com/api/v1/items/{item_id}", deadline=deadline)
        if not isinstance(data, dict) or not data.get("id"):
            raise TransportError("hackernews item not found", retryable=False, status_code=404)
        story_id = str(data.get("story_id") or data["id"])
        comments: list[Comment] = []

        def walk(node: dict[str, Any], depth: int) -> None:
            for child in node.get("children") or []:
                if not isinstance(child, dict):
                    continue
                text = html_to_text(child.get("text"))
                if text and child.get("id"):
                    comments.append(
                        Comment(
                            id=str(child["id"]),
                            author=str(child.get("author") or ""),
                            created_at=child.get("created_at"),
                            text=text,
                            url=f"https://news.ycombinator.com/item?id={child['id']}",
                            parent_id=str(child.get("parent_id") or "") or None,
                            depth=depth,
                            points=child.get("points"),
                        )
                    )
                walk(child, depth + 1)

        walk(data, 0)
        return ThreadEvidence(
            platform="hackernews",
            url=f"https://news.ycombinator.com/item?id={data['id']}",
            thread_id=str(data["id"]),
            title=str(data.get("title") or ""),
            author=str(data.get("author") or ""),
            created_at=data.get("created_at"),
            body=html_to_text(data.get("text")) or (f"Link: {data.get('url')}" if data.get("url") else ""),
            status="comments_read" if comments else "read",
            comments_total=len(comments),
            comments=comments,
            source_api="hn.algolia.com/api/v1/items" + ("" if story_id == str(data["id"]) else f" (story {story_id})"),
        )

    async def _v2ex_read(self, topic_id: str, deadline: float | None) -> ThreadEvidence:
        topics = await self._get("v2ex", f"https://www.v2ex.com/api/topics/show.json?id={topic_id}", deadline=deadline)
        if not isinstance(topics, list) or not topics or not isinstance(topics[0], dict):
            raise TransportError("v2ex topic not found", retryable=False, status_code=404)
        topic = topics[0]
        replies = await self._get("v2ex", f"https://www.v2ex.com/api/replies/show.json?topic_id={topic_id}", deadline=deadline)
        comments = []
        for index, reply in enumerate(replies if isinstance(replies, list) else [], start=1):
            if not isinstance(reply, dict) or not reply.get("id"):
                continue
            member = reply.get("member") if isinstance(reply.get("member"), dict) else {}
            comments.append(
                Comment(
                    id=str(reply["id"]),
                    author=str(member.get("username") or ""),
                    created_at=_iso(reply.get("created")),
                    text=str(reply.get("content") or "").strip(),
                    url=f"https://www.v2ex.com/t/{topic_id}#r_{reply['id']}",
                    depth=0,
                    points=index,  # floor number
                )
            )
        member = topic.get("member") if isinstance(topic.get("member"), dict) else {}
        return ThreadEvidence(
            platform="v2ex",
            url=f"https://www.v2ex.com/t/{topic_id}",
            thread_id=str(topic_id),
            title=str(topic.get("title") or ""),
            author=str(member.get("username") or ""),
            created_at=_iso(topic.get("created")),
            body=str(topic.get("content") or "").strip(),
            status="comments_read" if comments else "read",
            comments_total=int(topic.get("replies") or len(comments)),
            comments=comments,
            source_api="www.v2ex.com/api v1",
        )

    async def _se_read(self, ref: str, deadline: float | None) -> ThreadEvidence:
        site, question_id = ref.split(":", 1)
        q = await self._get(
            "stackexchange",
            f"https://api.stackexchange.com/2.3/questions/{question_id}?" + self._se_params(site=site, filter="withbody"),
            deadline=deadline,
        )
        items = (q or {}).get("items") or []
        if not items:
            raise TransportError("stack exchange question not found", retryable=False, status_code=404)
        question = items[0]
        answers = await self._get(
            "stackexchange",
            f"https://api.stackexchange.com/2.3/questions/{question_id}/answers?"
            + self._se_params(site=site, filter="withbody", sort="votes", order="desc", pagesize=50),
            deadline=deadline,
        )
        comments = []
        for answer in (answers or {}).get("items") or []:
            if not isinstance(answer, dict) or not answer.get("answer_id"):
                continue
            owner = answer.get("owner") if isinstance(answer.get("owner"), dict) else {}
            accepted = " [accepted]" if answer.get("is_accepted") else ""
            comments.append(
                Comment(
                    id=str(answer["answer_id"]),
                    author=html.unescape(str(owner.get("display_name") or "")) + accepted,
                    created_at=_iso(answer.get("creation_date")),
                    text=html_to_text(answer.get("body")),
                    url=f"{str(question.get('link') or '').split('#')[0]}#{answer['answer_id']}",
                    parent_id=str(question_id),
                    points=answer.get("score"),
                )
            )
        owner = question.get("owner") if isinstance(question.get("owner"), dict) else {}
        return ThreadEvidence(
            platform="stackexchange",
            url=str(question.get("link") or ""),
            thread_id=str(question_id),
            title=html.unescape(str(question.get("title") or "")),
            author=html.unescape(str(owner.get("display_name") or "")),
            created_at=_iso(question.get("creation_date")),
            body=html_to_text(question.get("body")),
            status="comments_read" if comments else "read",
            comments_total=int(question.get("answer_count") or len(comments)),
            comments=comments,
            source_api=f"api.stackexchange.com/2.3 site={site}",
        )

    async def _reddit_read(self, post_id: str, deadline: float | None) -> ThreadEvidence:
        token = await self._reddit_bearer(deadline)
        data = await self._get(
            "reddit",
            f"https://oauth.reddit.com/comments/{post_id}?limit=200&depth=6&raw_json=1",
            deadline=deadline,
            headers={"Authorization": f"Bearer {token}"},
        )
        if not isinstance(data, list) or len(data) < 2:
            raise TransportError("reddit thread not found", retryable=False, status_code=404)
        post = (((data[0] or {}).get("data") or {}).get("children") or [{}])[0].get("data") or {}
        comments: list[Comment] = []

        def walk(listing: Any, depth: int) -> None:
            for child in ((listing or {}).get("data") or {}).get("children") or []:
                if not isinstance(child, dict) or child.get("kind") != "t1":
                    continue
                body = child.get("data") or {}
                comments.append(
                    Comment(
                        id=str(body.get("id") or ""),
                        author=str(body.get("author") or ""),
                        created_at=_iso(body.get("created_utc")),
                        text=str(body.get("body") or ""),
                        url="https://www.reddit.com" + str(body.get("permalink") or ""),
                        parent_id=str(body.get("parent_id") or "") or None,
                        depth=depth,
                        points=body.get("score"),
                    )
                )
                if isinstance(body.get("replies"), dict):
                    walk(body["replies"], depth + 1)

        walk(data[1], 0)
        return ThreadEvidence(
            platform="reddit",
            url="https://www.reddit.com" + str(post.get("permalink") or f"/comments/{post_id}"),
            thread_id=post_id,
            title=str(post.get("title") or ""),
            author=str(post.get("author") or ""),
            created_at=_iso(post.get("created_utc")),
            body=str(post.get("selftext") or ""),
            status="comments_read" if comments else "read",
            comments_total=int(post.get("num_comments") or len(comments)),
            comments=comments,
            source_api="oauth.reddit.com",
        )


def render_thread(thread: ThreadEvidence) -> str:
    """Markdown view of a thread for clients that only read ``content``."""
    lines = [f"# {thread.title or thread.url}", ""]
    meta = " · ".join(x for x in (thread.author and f"by {thread.author}", thread.created_at, thread.source_api and f"via {thread.source_api}") if x)
    if meta:
        lines += [meta, ""]
    if thread.body:
        lines += [thread.body, ""]
    shown = len(thread.comments)
    lines += [f"## Comments ({shown} shown of {thread.comments_total if thread.comments_total is not None else shown})", ""]
    for comment in thread.comments:
        head = f"### [{comment.id}] {comment.author}"
        if comment.created_at:
            head += f" · {comment.created_at}"
        if comment.parent_id:
            head += f" · reply to {comment.parent_id}"
        lines += [head, comment.url, "", comment.text, ""]
    return "\n".join(lines).strip() + "\n"


def unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))
