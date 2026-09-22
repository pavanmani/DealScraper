"""Pluggable page fetch with block detection and optional scraping-API fallback."""

from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote, urlparse

log = logging.getLogger("dealscraper.fetcher")

try:
    from curl_cffi import requests as cffi_requests  # type: ignore

    _HAS_CURL_CFFI = True
except ImportError:
    _HAS_CURL_CFFI = False
    import requests as std_requests
else:
    import requests as std_requests  # still needed for API backends

TIMEOUT = 30
BLOCK_STATUS = {403, 429, 503}
DEFAULT_BACKENDS = ("direct", "scraperapi", "zenrows")

CAPTCHA_MARKERS = (
    "enter the characters you see below",
    "api-services-support@amazon.com",
    "sorry, we just need to make sure you're not a robot",
    "opfcaptcha",
    "/errors/validatecaptcha",
    "attention required",
    "cf-challenge",
)

SENTINELS: dict[str, tuple[str, ...]] = {
    "amazon": ("id=\"productTitle\"", "id='productTitle'", 'id="productTitle"'),
    "flipkart": ("__INITIAL_STATE__", "application/ld+json"),
    "generic": ("application/ld+json",),
}

HEADER_SETS: list[dict[str, str]] = [
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Sec-CH-UA": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Sec-CH-UA": '"Not;A=Brand";v="99", "Google Chrome";v="139", "Chromium";v="139"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Sec-CH-UA": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"macOS"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.7; rv:131.0) Gecko/20100101 Firefox/131.0"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Sec-CH-UA": '"Chromium";v="140", "Microsoft Edge";v="140", "Not=A?Brand";v="24"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Sec-CH-UA": '"Not;A=Brand";v="99", "Google Chrome";v="139", "Chromium";v="139"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Linux"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.8",
        "Sec-CH-UA": '"Not)A;Brand";v="8", "Chromium";v="138", "Google Chrome";v="138"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    },
]


@dataclass
class FetchResult:
    url: str
    html: str
    status: int
    backend: str
    blocked: bool
    reason: str | None = None


def site_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "amazon." in host:
        return "amazon"
    if "flipkart." in host:
        return "flipkart"
    return "generic"


def _headers_for(url: str) -> dict[str, str]:
    headers = dict(random.choice(HEADER_SETS))
    parsed = urlparse(url)
    headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    headers.pop("Accept-Encoding", None)
    return headers


def _looks_like_captcha(html: str) -> bool:
    lowered = html.lower()
    return any(marker in lowered for marker in CAPTCHA_MARKERS)


def _has_sentinel(html: str, site: str) -> bool:
    needles = SENTINELS.get(site, SENTINELS["generic"])
    return any(needle in html for needle in needles)


def is_blocked(status: int, html: str, site: str) -> tuple[bool, str | None]:
    if status in BLOCK_STATUS:
        return True, f"http_{status}"
    if status != 200:
        return True, f"http_{status}"
    if not html or len(html) < 2000:
        return True, "empty_or_tiny_body"
    if _looks_like_captcha(html):
        return True, "captcha"
    if not _has_sentinel(html, site):
        return True, "missing_sentinel"
    return False, None


def _delay_bounds() -> tuple[float, float]:
    lo = float(os.environ.get("DELAY_MIN", "3"))
    hi = float(os.environ.get("DELAY_MAX", "8"))
    if hi < lo:
        lo, hi = hi, lo
    return lo, hi


def jitter_delay() -> None:
    lo, hi = _delay_bounds()
    time.sleep(random.uniform(lo, hi))


def _fetch_direct(url: str) -> tuple[int, str]:
    headers = _headers_for(url)
    if _HAS_CURL_CFFI:
        resp = cffi_requests.get(
            url,
            headers=headers,
            timeout=TIMEOUT,
            impersonate="chrome",
            allow_redirects=True,
        )
    else:
        resp = std_requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
    return resp.status_code, resp.text or ""


def _fetch_scraperapi(url: str) -> tuple[int, str]:
    key = os.environ.get("SCRAPERAPI_KEY", "").strip()
    if not key:
        raise RuntimeError("SCRAPERAPI_KEY not set")
    api = (
        "https://api.scraperapi.com/"
        f"?api_key={quote(key)}&url={quote(url, safe='')}&country_code=in"
    )
    resp = std_requests.get(api, timeout=TIMEOUT + 30)
    return resp.status_code, resp.text or ""


def _fetch_zenrows(url: str) -> tuple[int, str]:
    key = os.environ.get("ZENROWS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ZENROWS_API_KEY not set")
    api = (
        "https://api.zenrows.com/v1/"
        f"?apikey={quote(key)}&url={quote(url, safe='')}"
        "&premium_proxy=true&proxy_country=in"
    )
    resp = std_requests.get(api, timeout=TIMEOUT + 30)
    return resp.status_code, resp.text or ""


BACKENDS: dict[str, Callable[[str], tuple[int, str]]] = {
    "direct": _fetch_direct,
    "scraperapi": _fetch_scraperapi,
    "zenrows": _fetch_zenrows,
}


def configured_backends() -> list[str]:
    raw = os.environ.get("FETCH_BACKENDS", ",".join(DEFAULT_BACKENDS))
    names = [part.strip().lower() for part in raw.split(",") if part.strip()]
    unknown = [n for n in names if n not in BACKENDS]
    if unknown:
        log.warning("unknown fetch backends ignored: %s", unknown)
    return [n for n in names if n in BACKENDS] or ["direct"]


def _backend_ready(name: str) -> bool:
    if name == "direct":
        return True
    if name == "scraperapi":
        return bool(os.environ.get("SCRAPERAPI_KEY", "").strip())
    if name == "zenrows":
        return bool(os.environ.get("ZENROWS_API_KEY", "").strip())
    return False


def fetch_page(url: str, site: str | None = None) -> FetchResult:
    site = site or site_from_url(url)
    last: FetchResult | None = None
    for name in configured_backends():
        if not _backend_ready(name):
            log.info("skipping backend %s (not configured)", name)
            continue
        try:
            status, html = BACKENDS[name](url)
        except Exception as exc:
            log.warning("backend %s failed for %s: %s", name, url, exc)
            last = FetchResult(url, "", 0, name, True, f"error:{exc}")
            continue
        blocked, reason = is_blocked(status, html, site)
        result = FetchResult(url, html, status, name, blocked, reason)
        if not blocked:
            log.info("fetched %s via %s (%s bytes)", url, name, len(html))
            return result
        log.warning("backend %s blocked (%s) for %s", name, reason, url)
        last = result
    if last is None:
        return FetchResult(url, "", 0, "none", True, "no_backends")
    return last
