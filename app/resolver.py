from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


class ResolveError(Exception):
    """A URL could not be safely resolved through ordinary redirects."""


REDIRECT_CODES = {301, 302, 303, 307, 308}
USER_AGENT = "Mozilla/5.0 (compatible; ShortLinkResolver/1.1)"
logger = logging.getLogger("resolver")
MAX_HTML_BYTES = 256 * 1024
_REFRESH_RE = re.compile(
    r"""^\s*(\d+(?:\.\d+)?)\s*;\s*url\s*=\s*['"]?([^'"]+?)['"]?\s*$""",
    re.IGNORECASE,
)


class _MetaRefreshParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refresh: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta" or self.refresh is not None:
            return
        values = {key.lower(): value for key, value in attrs if value is not None}
        if values.get("http-equiv", "").strip().lower() == "refresh":
            self.refresh = values.get("content", "")


def _refresh_target(value: str | None) -> tuple[str, float] | None:
    if not value:
        return None
    match = _REFRESH_RE.match(value)
    if not match:
        return None
    try:
        delay = float(match.group(1))
    except ValueError:
        return None
    target = match.group(2).strip()
    return (target, delay) if target else None


def _normalize_url(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 2048:
        raise ResolveError("That link is empty or too long.")
    if any(ord(ch) < 32 for ch in value):
        raise ResolveError("The link contains invalid characters.")
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ResolveError("Only HTTP and HTTPS links are supported.")
    if not parts.hostname or parts.username is not None or parts.password is not None:
        raise ResolveError("That link is not a valid public URL.")
    try:
        port = parts.port
    except ValueError as exc:
        raise ResolveError("The link contains an invalid port.") from exc
    if port not in (None, 80, 443):
        raise ResolveError("Only standard HTTP/HTTPS ports are supported.")
    host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ResolveError("Private/local network links are not allowed.")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


async def _check_public_host(url: str) -> None:
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        literal_ip = ipaddress.ip_address(host)
        addresses = [literal_ip]
    except ValueError:
        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo,
                host,
                parts.port or (443 if parts.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise ResolveError("Could not look up that link's host.") from exc
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0].split("%", 1)[0]))
            except ValueError:
                continue
    if not addresses or any(not address.is_global for address in addresses):
        raise ResolveError("Private or reserved network destinations are blocked.")


async def _read_limited_html(response: httpx.Response) -> str:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        remaining = MAX_HTML_BYTES - total
        if remaining <= 0:
            break
        piece = chunk[:remaining]
        chunks.append(piece)
        total += len(piece)
        if len(chunk) > remaining or total >= MAX_HTML_BYTES:
            break
    return b"".join(chunks).decode("utf-8", errors="replace")


async def resolve_url(raw_url: str, max_redirects: int = 10, timeout: float = 12.0) -> tuple[str, int]:
    current = _normalize_url(raw_url)
    visited: set[str] = set()
    limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)
    timeout_config = httpx.Timeout(timeout, connect=min(timeout, 6.0))
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=timeout_config,
        limits=limits,
        verify=True,
        trust_env=False,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
    ) as client:
        for hop in range(max_redirects + 1):
            current = _normalize_url(current)
            if current in visited:
                raise ResolveError("A redirect loop was detected.")
            visited.add(current)
            await _check_public_host(current)
            try:
                async with client.stream("GET", current) as response:
                    logger.info(
                        "Resolver response host=%s status=%s content_type=%s location_present=%s refresh_present=%s",
                        urlsplit(str(response.url)).hostname,
                        response.status_code,
                        response.headers.get("content-type", "unknown").split(";", 1)[0],
                        bool(response.headers.get("location")),
                        bool(response.headers.get("refresh")),
                    )
                    if response.status_code in REDIRECT_CODES:
                        location = response.headers.get("location")
                        if not location:
                            raise ResolveError("The site returned a redirect without a destination.")
                        if hop >= max_redirects:
                            raise ResolveError("Too many redirects. Try a different link.")
                        current = urljoin(str(response.url), location)
                        continue

                    if 200 <= response.status_code < 300:
                        content_type = response.headers.get("content-type", "").lower()
                        if "html" in content_type or "xhtml" in content_type:
                            refresh = _refresh_target(response.headers.get("refresh"))
                            if refresh is None:
                                html = await _read_limited_html(response)
                                parser = _MetaRefreshParser()
                                parser.feed(html)
                                refresh = _refresh_target(parser.refresh)
                                logger.info(
                                    "Resolver HTML inspected host=%s bytes_read=%s meta_refresh_found=%s",
                                    urlsplit(str(response.url)).hostname,
                                    len(html.encode("utf-8", errors="ignore")),
                                    bool(refresh),
                                )
                                page_text = html.lower()
                                if refresh is None and "please wait" in page_text and "opening link" in page_text:
                                    raise ResolveError(
                                        "This shortener returned an intermediate waiting page. "
                                        "It may require JavaScript or manual verification; the bot does not skip ad gates."
                                    )

                            if refresh is not None:
                                target, delay = refresh
                                if delay > 0:
                                    raise ResolveError(
                                        "This page uses a timed redirect. The bot does not skip ad timers "
                                        "or waiting periods."
                                    )
                                if hop >= max_redirects:
                                    raise ResolveError("Too many redirects. Try a different link.")
                                current = urljoin(str(response.url), target)
                                continue
                        return str(response.url), hop

                    if response.status_code in (401, 403, 407, 429):
                        raise ResolveError(
                            f"The destination blocked the request (HTTP {response.status_code}). "
                            "This bot does not bypass login, CAPTCHA, ad gates, or rate limits."
                        )
                    raise ResolveError(f"The destination returned HTTP {response.status_code}.")
            except httpx.TimeoutException as exc:
                raise ResolveError("The link timed out. Please try again later.") from exc
            except httpx.RequestError as exc:
                raise ResolveError("Could not connect to that link safely.") from exc
    raise ResolveError("Could not resolve that link.")
