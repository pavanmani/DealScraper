"""Per-site product extraction. Prefer JSON-LD / page JSON over hashed CSS classes."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup

log = logging.getLogger("dealscraper.extractors")

AMAZON_PRICE_SELECTORS = (
    "#corePriceDisplay_desktop_feature_div .a-price-whole",
    "#corePrice_feature_div .a-offscreen",
    ".priceToPay .a-price-whole",
    "#tp_price_block_total_price_ww",
    "#corePriceDisplay_desktop_feature_div span.a-price .a-offscreen",
)

FLIPKART_BUYBOX_SELECTORS = (
    "div.C7fEHH",
    "div.Nx9bqj",
    "div[class*='CxhGGd']",
)

OOS_PATTERNS = (
    "currently unavailable",
    "out of stock",
    "sold out",
    "temporarily out of stock",
)

IN_STOCK_PATTERNS = (
    "in stock",
    "add to cart",
    "buy now",
)


@dataclass
class ExtractResult:
    title: str | None
    price: int | None
    in_stock: bool | None
    source: str | None
    keyword_mismatch: list[str]


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def sanitize_price(raw: Any, *, min_price: int | None = None, max_price: int | None = None) -> int | None:
    min_price = env_int("PRICE_MIN", 50000) if min_price is None else min_price
    max_price = env_int("PRICE_MAX", 400000) if max_price is None else max_price
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = int(raw)
    else:
        digits = re.sub(r"[^\d]", "", str(raw))
        if not digits:
            return None
        value = int(digits)
        # Drop trailing paise (e.g. 17985700) if that is the only way it fits the band.
        if value > max_price and value % 100 == 0:
            halved = value // 100
            if min_price <= halved <= max_price:
                value = halved
    if value < min_price or value > max_price:
        log.debug("price %s outside sanity band %s-%s", value, min_price, max_price)
        return None
    return value


def _text(el) -> str | None:
    if el is None:
        return None
    text = el.get_text(" ", strip=True)
    return text or None


def _json_ld_blocks(soup: BeautifulSoup) -> list[Any]:
    blocks: list[Any] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            blocks.extend(data)
        else:
            blocks.append(data)
    return blocks


def price_from_json_ld(soup: BeautifulSoup) -> tuple[int | None, str | None, bool | None, str | None]:
    """Read price from the top-level Product node only — do not walk nested carousels."""
    title = None
    for block in _json_ld_blocks(soup):
        if not isinstance(block, dict):
            continue
        types = block.get("@type")
        type_list = types if isinstance(types, list) else [types]
        type_list = [str(t).lower() for t in type_list if t]
        if not title:
            name = block.get("name")
            if isinstance(name, str):
                title = name
        if not any("product" in t for t in type_list):
            continue
        offers = block.get("offers")
        candidates = offers if isinstance(offers, list) else [offers]
        for offer in candidates:
            if not isinstance(offer, dict):
                continue
            price = sanitize_price(offer.get("price") or offer.get("lowPrice"))
            if price is None:
                continue
            avail = offer.get("availability")
            in_stock = None
            if isinstance(avail, str):
                lowered = avail.lower()
                if "outofstock" in lowered or "soldout" in lowered:
                    in_stock = False
                elif "instock" in lowered or "instoreonly" in lowered or "preorder" in lowered:
                    in_stock = True
            return price, title, in_stock, "json_ld"
    return None, title, None, None


def amazon_in_stock(soup: BeautifulSoup) -> bool | None:
    avail = soup.select_one("#availability")
    text = (_text(avail) or "").lower()
    if not text:
        oos = soup.select_one("#outOfStock")
        text = (_text(oos) or "").lower()
    if not text:
        return None
    if any(p in text for p in OOS_PATTERNS):
        return False
    if any(p in text for p in IN_STOCK_PATTERNS):
        return True
    return None


def extract_amazon(html: str) -> ExtractResult:
    soup = BeautifulSoup(html, "lxml")
    title = _text(soup.select_one("#productTitle"))
    in_stock = amazon_in_stock(soup)
    for selector in AMAZON_PRICE_SELECTORS:
        price = sanitize_price(_text(soup.select_one(selector)))
        if price is not None:
            return ExtractResult(title, price, in_stock, f"css:{selector}", [])
    price, ld_title, ld_stock, source = price_from_json_ld(soup)
    if in_stock is None:
        in_stock = ld_stock
    return ExtractResult(title or ld_title, price, in_stock, source, [])


def _first_final_price(html: str) -> int | None:
    """First `finalPrice` in the document is the PDP product; carousels come later."""
    match = re.search(r'"finalPrice"\s*:\s*(\d+)', html)
    if not match:
        return None
    return sanitize_price(int(match.group(1)))


def _flipkart_initial_state(html: str) -> Any | None:
    match = re.search(
        r"window\.__INITIAL_STATE__\s*=\s*(\{.*?})\s*;?\s*</script>",
        html,
        flags=re.DOTALL,
    )
    if not match:
        match = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*)", html, flags=re.DOTALL)
        if not match:
            return None
        blob = match.group(1)
        end = blob.rfind("}</script>")
        if end != -1:
            blob = blob[: end + 1]
        else:
            last = blob.rfind("}")
            blob = blob[: last + 1] if last != -1 else blob
    else:
        blob = match.group(1)
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        log.debug("Flipkart __INITIAL_STATE__ is not strict JSON; using regex fallback")
        return None


def _collect_final_prices(node: Any, acc: list[int]) -> None:
    if isinstance(node, dict):
        if "finalPrice" in node:
            price = sanitize_price(node.get("finalPrice"))
            if price is not None:
                acc.append(price)
        for value in node.values():
            _collect_final_prices(value, acc)
    elif isinstance(node, list):
        for item in node:
            _collect_final_prices(item, acc)


def _flipkart_page_title(soup: BeautifulSoup, state: Any) -> str | None:
    for selector in ("h1 span", "h1", "span.VU-ZEz", "span.B_NuCI"):
        title = _text(soup.select_one(selector))
        if title:
            return title
    if isinstance(state, dict):
        seo = state.get("seo") or {}
        if isinstance(seo, dict) and seo.get("pageTitle"):
            return str(seo["pageTitle"])
    return None


def _flipkart_buybox_price(soup: BeautifulSoup) -> int | None:
    for selector in FLIPKART_BUYBOX_SELECTORS:
        el = soup.select_one(selector)
        if el is None:
            continue
        match = re.search(r"[₹Rs\.]*\s*([\d,]+)", el.get_text(" ", strip=True))
        if match:
            price = sanitize_price(match.group(1))
            if price is not None:
                return price
    return None


def extract_flipkart(html: str) -> ExtractResult:
    soup = BeautifulSoup(html, "lxml")
    price, ld_title, ld_stock, source = price_from_json_ld(soup)
    page_title = _flipkart_page_title(soup, None)
    title = ld_title or page_title
    if page_title and ld_title and len(page_title) > len(ld_title):
        title = page_title
    in_stock = ld_stock
    if source and price is not None:
        return ExtractResult(title, price, in_stock, source, [])

    state = _flipkart_initial_state(html)
    if not title:
        title = _flipkart_page_title(soup, state)

    first = _first_final_price(html)
    if first is not None:
        return ExtractResult(title, first, in_stock, "initial_state.finalPrice", [])

    if state is not None:
        prices: list[int] = []
        _collect_final_prices(state, prices)
        if prices:
            return ExtractResult(title, prices[0], in_stock, "initial_state.walk", [])

    buybox = _flipkart_buybox_price(soup)
    if buybox is not None:
        return ExtractResult(title, buybox, in_stock, "buybox_regex", [])
    return ExtractResult(title, None, in_stock, None, [])


def extract_generic(html: str) -> ExtractResult:
    soup = BeautifulSoup(html, "lxml")
    price, title, in_stock, source = price_from_json_ld(soup)
    return ExtractResult(title, price, in_stock, source, [])


def check_keywords(title: str | None, keywords: list[str] | None) -> list[str]:
    if not keywords:
        return []
    hay = (title or "").lower()
    missing = [kw for kw in keywords if kw.lower() not in hay]
    return missing


def extract_product(
    html: str,
    site: str,
    match_keywords: list[str] | None = None,
    extra_text: str | None = None,
) -> ExtractResult:
    site = (site or "generic").lower()
    if site == "amazon":
        result = extract_amazon(html)
    elif site == "flipkart":
        result = extract_flipkart(html)
    else:
        result = extract_generic(html)
    hay = " ".join(part for part in (result.title, extra_text) if part)
    result.keyword_mismatch = check_keywords(hay, match_keywords)
    return result


def extract_search_results(html: str, site: str) -> list[dict[str, Any]]:
    """Phase 2 hook: search-page extraction is not implemented yet."""
    raise NotImplementedError("search discovery is planned for phase 2")
