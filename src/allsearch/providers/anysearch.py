"""AnySearch JSON-RPC vertical directory/search adapter."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from allsearch.cache import MemoryCache
from allsearch.config import AnySearchConfig
from allsearch.errors import AuthError, ProviderContractError, ProviderUnavailableError
from allsearch.health import HealthRegistry
from allsearch.models import ProviderResultItem, ProviderSearchResult
from allsearch.transport import HttpTransport

URL_RE = re.compile(r"https?://[^\s\)\]\>\"']+")

# Columns documented by the AnySearch CLI help for `get_sub_domains`:
# domain, sub_domain, description, query_format, params_schema, zone
_DIRECTORY_COLUMNS = (
    "domain",
    "sub_domain",
    "description",
    "query_format",
    "params_schema",
    "zone",
)


def _split_markdown_table(text: str) -> list[dict[str, str]]:
    """Parse a GitHub-flavored Markdown table into a list of row dicts."""
    rows: list[dict[str, str]] = []
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    header: list[str] | None = None
    for line in lines:
        if not line.startswith("|") or not line.rstrip().endswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        # separator row like | --- | --- |
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if cells):
            continue
        if header is None:
            header = cells
            continue
        if header is None or len(cells) != len(header):
            continue
        rows.append({header[i]: cells[i] for i in range(len(header))})
    return rows


def _parse_params_schema(raw: str) -> list[str]:
    """Extract required param names from a params_schema cell.

    Tolerates forms like: "ticker (required), as_of", a JSON array of param
    objects, or "ticker, period". Returns required subset when a marker exists.
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    required: list[str] = []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("key") or "").strip()
                if not name:
                    continue
                if _is_marked_required(item):
                    required.append(name)
        return required
    for token in re.split(r"[,;\n]", raw):
        token = token.strip()
        if not token:
            continue
        m = re.match(r"([A-Za-z0-9_\-]+)\s*(?:\((.*?)\))?", token)
        if not m:
            continue
        name = m.group(1)
        marker = (m.group(2) or "").lower()
        if "required" in marker or "mandatory" in marker:
            required.append(name)
    return required


def _is_marked_required(item: dict[str, Any]) -> bool:
    for key in ("required", "mandatory"):
        if item.get(key) is True:
            return True
        if str(item.get(key) or "").lower() in {"true", "required", "yes", "mandatory"}:
            return True
    name = str(item.get("name") or item.get("key") or "")
    return "required" in name.lower() or "mandatory" in name.lower()


def _split_markdown_sections(text: str) -> list[dict[str, Any]]:
    """Parse the current AnySearch `### sub_domain` capability format."""
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_parameters = False
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        heading = re.match(r"^###\s+([A-Za-z0-9_.-]+)\s*$", line)
        if heading:
            if current is not None:
                entries.append(current)
            sub = heading.group(1)
            current = {
                "name": sub,
                "sub_domain": sub,
                "description": "",
                "query_format": "",
                "zone": "",
                "required_params": [],
                "param_descriptions": {},
            }
            in_parameters = False
            continue
        if current is None:
            continue
        if line.lower() == "**parameters:**":
            in_parameters = True
            continue
        if in_parameters:
            param = re.match(
                r"^-\s+`([^`]+)`\s*(?:\((required|optional)\))?\s*:\s*(.*)$",
                line,
                re.I,
            )
            if param:
                name, marker, description = param.groups()
                current["param_descriptions"][name] = description.strip()
                if (marker or "").lower() == "required":
                    current["required_params"].append(name)
                continue
            # Example JSON and continuation lines are not parameter definitions.
            continue
        if line and not line.startswith("##"):
            description = current.get("description") or ""
            current["description"] = f"{description} {line}".strip()
    if current is not None:
        entries.append(current)
    return entries


def _infer_required_param_values(
    required: list[str],
    query: str,
    *,
    sub_domain: str,
) -> dict[str, str]:
    """Infer only high-confidence structured identifiers from the query."""
    params = {name: "" for name in required}
    cve_match = re.search(r"\bCVE-\d{4}-\d{4,}\b", query, re.I)
    doi_match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", query, re.I)
    ipv4_match = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", query)
    ticker_match = re.search(r"(?:\$|NASDAQ:|NYSE:)([A-Z]{1,6})\b", query, re.I)

    for name in required:
        lowered = name.lower()
        if cve_match:
            cve = cve_match.group(0).upper()
            if lowered in {"cve", "cve_id", "id", "value"}:
                params[name] = cve
            elif lowered == "type" and ("vuln" in sub_domain or "cve" in sub_domain):
                params[name] = "cve"
        if doi_match and lowered in {"doi", "id", "value"}:
            params[name] = doi_match.group(0)
        if ipv4_match and lowered in {"ip", "ioc", "value"}:
            params[name] = ipv4_match.group(0)
        if ticker_match and lowered in {"ticker", "symbol", "code"}:
            params[name] = ticker_match.group(1).upper()
    return params


def parse_directory_markdown(text: str) -> list[dict[str, Any]]:
    """Parse legacy table or current section-based AnySearch directories."""
    rows = _split_markdown_table(text)
    entries: list[dict[str, Any]] = []
    for row in rows:
        sub = (
            row.get("sub_domain")
            or row.get("name")
            or row.get("subdomain")
            or ""
        )
        sub = (sub or "").strip()
        if not sub:
            values = list(row.values())
            if len(values) >= 2:
                sub = values[1].strip()
            if not sub:
                continue
        params_raw = (
            row.get("params_schema")
            or row.get("params")
            or row.get("parameters")
            or ""
        )
        entry: dict[str, Any] = {
            "name": sub,
            "sub_domain": sub,
            "description": (row.get("description") or "").strip(),
            "query_format": (row.get("query_format") or "").strip(),
            "zone": (row.get("zone") or "").strip(),
            "required_params": _parse_params_schema(params_raw),
        }
        entries.append(entry)
    if entries:
        return entries
    return _split_markdown_sections(text)


def _parse_search_markdown(
    text: str,
    *,
    domain: str | None,
) -> list[ProviderResultItem]:
    """Parse current AnySearch `### N. title` search-result Markdown."""
    results: list[ProviderResultItem] = []
    current_title = ""
    current_url = ""
    content_lines: list[str] = []

    def flush() -> None:
        nonlocal current_title, current_url, content_lines
        url = current_url.strip().rstrip(".,;")
        if not current_title and not url:
            return
        # Only accept absolute HTTP(S) URLs with a real hostname.
        valid_url = ""
        if url:
            from urllib.parse import urlparse

            try:
                parsed = urlparse(url)
                hostname = parsed.hostname or ""
                if (
                    parsed.scheme in {"http", "https"}
                    and hostname
                    and re.fullmatch(r"[A-Za-z0-9.-]+", hostname)
                    and any(ch.isalnum() for ch in hostname)
                ):
                    valid_url = url
            except ValueError:
                valid_url = ""
        snippet = "\n".join(line for line in content_lines if line).strip()
        results.append(
            ProviderResultItem(
                title=current_title or valid_url,
                url=valid_url,
                snippet=snippet[:2000],
                content=snippet[:8000] or None,
                source_type="vertical",
                provider="anysearch",
                vertical=domain,
            )
        )

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        heading = re.match(r"^###\s+\d+\.\s+(.+?)\s*$", line)
        if heading:
            flush()
            current_title = heading.group(1).strip()
            current_url = ""
            content_lines = []
            continue
        if not current_title:
            continue
        url_line = re.match(r"^-\s+\*\*URL\*\*:\s*(\S+)\s*$", line, re.I)
        if url_line:
            current_url = url_line.group(1)
            continue
        if line:
            # Strip a leading list marker from the result excerpt only.
            content_lines.append(re.sub(r"^-\s+", "", line))
    flush()
    return results


def _has_structured_entries(directory: Any) -> bool:
    if isinstance(directory, list):
        return any(isinstance(e, dict) and (e.get("name") or e.get("sub_domain")) for e in directory)
    if isinstance(directory, dict):
        for key in ("sub_domains", "subdomains", "items", "data"):
            block = directory.get(key)
            if isinstance(block, list) and any(
                isinstance(e, dict) and (e.get("name") or e.get("sub_domain")) for e in block
            ):
                return True
        return bool(directory.get("name") or directory.get("sub_domain"))
    return False


class AnySearchProvider:
    name = "anysearch"

    def __init__(
        self,
        config: AnySearchConfig,
        transport: HttpTransport,
        health: HealthRegistry | None = None,
        cache: MemoryCache | None = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self.health = health
        self.cache = cache or MemoryCache()
        self._rpc_id = 0

    def configured(self) -> bool:
        return self.config.configured()

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def build_rpc(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }

    @staticmethod
    def extract_text(payload: dict[str, Any]) -> str:
        if "error" in payload and payload["error"]:
            err = payload["error"]
            if isinstance(err, dict):
                msg = err.get("message") or str(err)
            else:
                msg = str(err)
            raise ProviderContractError(f"AnySearch RPC error: {msg}", provider="anysearch")
        result = payload.get("result") or {}
        content = result.get("content") or []
        texts: list[str] = []
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(str(item.get("text") or ""))
        if texts:
            return "\n".join(texts)
        if isinstance(result, (dict, list)):
            return json.dumps(result, ensure_ascii=False)
        return str(result)

    def parse_results(self, text: str, *, domain: str | None = None) -> tuple[list[ProviderResultItem], list[str]]:
        warnings: list[str] = []
        results: list[ProviderResultItem] = []
        stripped = (text or "").strip()
        if not stripped:
            return results, warnings

        data: Any = None
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = None

        def _from_dict(item: dict[str, Any]) -> ProviderResultItem | None:
            url = str(item.get("url") or item.get("link") or "").strip()
            title = str(item.get("title") or item.get("name") or url)
            snippet = str(
                item.get("snippet")
                or item.get("content")
                or item.get("description")
                or item.get("summary")
                or ""
            )
            if not url and not title and not snippet:
                return None
            return ProviderResultItem(
                title=title or url,
                url=url,
                snippet=snippet,
                content=item.get("content") if isinstance(item.get("content"), str) else None,
                published_at=item.get("published_at") or item.get("date"),
                source_type="vertical",
                provider=self.name,
                vertical=domain,
                metadata={k: v for k, v in item.items() if k in {"score", "source", "id"}},
            )

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    parsed = _from_dict(item)
                    if parsed:
                        results.append(parsed)
            if results:
                return results, warnings
        elif isinstance(data, dict):
            # common envelopes
            for key in ("results", "items", "data", "records"):
                block = data.get(key)
                if isinstance(block, list):
                    for item in block:
                        if isinstance(item, dict):
                            parsed = _from_dict(item)
                            if parsed:
                                results.append(parsed)
            if not results and (data.get("url") or data.get("title")):
                parsed = _from_dict(data)
                if parsed:
                    results.append(parsed)
            if results:
                return results, warnings

        # Current AnySearch result format: Markdown sections with title, URL, excerpt.
        markdown_results = _parse_search_markdown(stripped, domain=domain)
        if markdown_results:
            return markdown_results, warnings

        # Last-resort fallback: keep text evidence + extract only valid URLs.
        warnings.append("anysearch_unstructured_text")
        urls: list[str] = []
        from urllib.parse import urlparse

        for candidate in URL_RE.findall(stripped):
            candidate = candidate.rstrip(".,;*")
            try:
                parsed = urlparse(candidate)
                hostname = parsed.hostname or ""
                if (
                    parsed.scheme in {"http", "https"}
                    and hostname
                    and re.fullmatch(r"[A-Za-z0-9.-]+", hostname)
                    and any(ch.isalnum() for ch in hostname)
                ):
                    if candidate not in urls:
                        urls.append(candidate)
            except ValueError:
                continue
        if urls:
            for u in urls[:10]:
                results.append(
                    ProviderResultItem(
                        title=u,
                        url=u.rstrip(".,;"),
                        snippet=stripped[:400],
                        source_type="vertical",
                        provider=self.name,
                        vertical=domain,
                    )
                )
        else:
            results.append(
                ProviderResultItem(
                    title=f"AnySearch {domain or 'result'}",
                    url="",
                    snippet=stripped[:800],
                    content=stripped[:4000],
                    source_type="vertical",
                    provider=self.name,
                    vertical=domain,
                )
            )
        return results, warnings

    async def _call(self, tool_name: str, arguments: dict[str, Any], *, deadline_seconds: float | None) -> str:
        if not self.configured():
            raise ProviderUnavailableError("AnySearch disabled", provider=self.name)
        if self.health:
            self.health.ensure_closed_or_raise(self.name)
        body = self.build_rpc(tool_name, arguments)
        started = time.perf_counter()
        try:
            response = await self.transport.request_json(
                "POST",
                self.config.endpoint,
                headers=self._headers(),
                json=body,
                deadline_seconds=deadline_seconds,
                provider=self.name,
            )
            text = self.extract_text(response)
            if self.health:
                self.health.record_success(self.name, int((time.perf_counter() - started) * 1000))
            return text
        except AuthError:
            if self.health:
                self.health.record_failure(self.name, "auth_error", retryable=False)
            raise
        except Exception as exc:
            if self.health:
                code = getattr(exc, "code", "error")
                retryable = bool(getattr(exc, "retryable", True))
                self.health.record_failure(self.name, str(code), retryable=retryable)
            raise

    async def get_sub_domains(self, domain: str, *, deadline_seconds: float | None = None) -> Any:
        cache_key = self.cache.make_key(
            "anysearch_directory",
            {"domain": domain, "endpoint": self.config.endpoint},
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        text = await self._call("get_sub_domains", {"domain": domain}, deadline_seconds=deadline_seconds)
        data: Any
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # The documented shape is a Markdown table with columns:
            # domain, sub_domain, description, query_format, params_schema, zone
            data = {"_markdown": text, "sub_domains": parse_directory_markdown(text)}
        else:
            # If the JSON already lacks structured entries but we have raw text,
            # also attempt a Markdown parse so downstream selection works.
            if isinstance(data, dict) and not _has_structured_entries(data):
                md = data.get("raw") or text
                data = {**data, "_markdown": md, "sub_domains": parse_directory_markdown(md)}
        self.cache.set(cache_key, data, self.config.directory_ttl_seconds)
        return data

    def invalidate_directory(self, domain: str) -> None:
        key = self.cache.make_key(
            "anysearch_directory",
            {"domain": domain, "endpoint": self.config.endpoint},
        )
        self.cache.delete(key)

    def select_sub_domain(self, directory: Any, query: str) -> tuple[str | None, dict[str, str]]:
        """Pick a sub_domain and required params best-effort from directory payload."""
        entries: list[dict[str, Any]] = []
        if isinstance(directory, list):
            entries = [e for e in directory if isinstance(e, dict)]
        elif isinstance(directory, dict):
            for key in ("sub_domains", "subdomains", "items", "data"):
                block = directory.get(key)
                if isinstance(block, list):
                    entries = [e for e in block if isinstance(e, dict)]
                    break
            if not entries and directory.get("name"):
                entries = [directory]

        if not entries:
            return None, {}

        q = query.lower()
        best = entries[0]
        best_score = -1
        for entry in entries:
            name = str(entry.get("name") or entry.get("sub_domain") or "").lower()
            desc = str(entry.get("description") or "").lower()
            score = 0
            if name and name in q:
                score += 5
            for token in re.findall(r"[a-z0-9_\-]{3,}", name):
                if token in q:
                    score += 2
            for token in re.findall(r"[a-z0-9_\-]{4,}", desc):
                if token in q:
                    score += 1
            if score > best_score:
                best_score = score
                best = entry

        sub = str(best.get("name") or best.get("sub_domain") or "") or None
        if not sub:
            return None, {}
        required = (
            best.get("required_params")
            or best.get("required")
            or best.get("params")
            or []
        )
        params: dict[str, str] = {}
        if isinstance(required, dict):
            params = _infer_required_param_values(
                [str(k) for k in required],
                query,
                sub_domain=sub,
            )
        elif isinstance(required, list):
            names: list[str] = []
            for item in required:
                if isinstance(item, str):
                    names.append(item)
                elif isinstance(item, dict) and item.get("name"):
                    names.append(str(item["name"]))
            params = _infer_required_param_values(names, query, sub_domain=sub)
        return sub, params

    async def search(
        self,
        query: str,
        *,
        max_results: int = 8,
        domain: str | None = None,
        sub_domain: str | None = None,
        sub_domain_params: dict[str, str] | None = None,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        deadline_seconds: float | None = None,
        **_: Any,
    ) -> ProviderSearchResult:
        _ = include_domains, exclude_domains
        if not self.configured():
            raise ProviderUnavailableError("AnySearch disabled", provider=self.name)

        arguments: dict[str, Any] = {
            "query": query,
            "max_results": max(1, min(int(max_results), self.config.max_results, 10)),
        }
        warnings: list[str] = []
        chosen_domain = domain
        if chosen_domain:
            directory = None
            try:
                directory = await self.get_sub_domains(chosen_domain, deadline_seconds=deadline_seconds)
            except Exception:
                warnings.append("anysearch_directory_failed")

            if sub_domain is None and directory is not None:
                sub_domain, inferred_params = self.select_sub_domain(directory, query)
                if sub_domain_params is None:
                    sub_domain_params = inferred_params
            if sub_domain:
                arguments["domain"] = chosen_domain
                arguments["sub_domain"] = sub_domain
                if sub_domain_params:
                    arguments["sub_domain_params"] = sub_domain_params
            else:
                # Vertical search REQUIRES a valid sub_domain per the AnySearch
                # contract. Sending a search with only `domain` triggers a backend
                # validation error. Refuse gracefully instead.
                warnings.append("anysearch_no_sub_domain_available")
                return ProviderSearchResult(
                    provider=self.name,
                    query=query,
                    answer="",
                    results=[],
                    citations=[],
                    raw_warnings=warnings,
                    latency_ms=0,
                )

        started = time.perf_counter()
        try:
            text = await self._call("search", arguments, deadline_seconds=deadline_seconds)
        except ProviderContractError:
            # invalidate directory once and retry if vertical
            if chosen_domain:
                self.invalidate_directory(chosen_domain)
                try:
                    directory = await self.get_sub_domains(chosen_domain, deadline_seconds=deadline_seconds)
                    sub_domain, inferred_params = self.select_sub_domain(directory, query)
                    if sub_domain:
                        arguments["sub_domain"] = sub_domain
                    if inferred_params and not sub_domain_params:
                        arguments["sub_domain_params"] = inferred_params
                    text = await self._call("search", arguments, deadline_seconds=deadline_seconds)
                    warnings.append("anysearch_directory_reloaded")
                except Exception:
                    raise
            else:
                raise

        results, parse_warnings = self.parse_results(text, domain=chosen_domain)
        warnings.extend(parse_warnings)
        citations = [{"title": r.title, "url": r.url} for r in results if r.url]
        return ProviderSearchResult(
            provider=self.name,
            query=query,
            answer="",
            results=results,
            citations=citations,
            raw_warnings=warnings,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
