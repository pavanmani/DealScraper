"""Orchestrate fetch → extract → alert → persist for tracked laptop listings."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from extractors import extract_product
from fetcher import fetch_page, jitter_delay, site_from_url
from notifier import notify
from state import (
    DEFAULT_STATE_PATH,
    find_product,
    load_state,
    record_alert,
    record_failure,
    record_out_of_stock,
    record_success,
    save_state,
)

log = logging.getLogger("dealscraper")

DEBUG_DIR = Path(__file__).resolve().parent / "debug"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def should_alert(
    product: dict[str, Any],
    new_price: int,
    in_stock: bool | None,
    *,
    was_in_stock: bool | None,
) -> tuple[bool, str]:
    if in_stock is False:
        return False, "out_of_stock"

    target = int(product["target_price"])
    old = product.get("last_price")
    old_int = int(old) if old is not None else None
    lowest = product.get("lowest_seen")
    lowest_price = (
        int(lowest["price"]) if isinstance(lowest, dict) and lowest.get("price") is not None else None
    )

    min_drop_abs = _env_int("MIN_DROP_ABS", 2000)
    min_drop_pct = _env_float("MIN_DROP_PCT", 0.03)
    cooldown_h = _env_float("ALERT_COOLDOWN_HOURS", 24)

    is_new_low = lowest_price is not None and new_price < lowest_price
    at_or_below_target = new_price <= target
    meaningful_drop = False
    if old_int is not None and new_price < old_int:
        drop = old_int - new_price
        threshold = max(min_drop_abs, int(old_int * min_drop_pct))
        meaningful_drop = drop >= threshold

    back_in_stock = was_in_stock is False and in_stock is not False and at_or_below_target

    if at_or_below_target and meaningful_drop:
        reason = "below_target_and_drop"
    elif back_in_stock:
        reason = "back_in_stock_below_target"
    elif at_or_below_target:
        reason = "below_target"
    elif meaningful_drop:
        reason = "price_drop"
    else:
        return False, "no_trigger"

    last_alert = product.get("last_alert")
    if is_new_low:
        return True, reason

    if last_alert and isinstance(last_alert, dict):
        last_at = _parse_iso(last_alert.get("at"))
        last_price = last_alert.get("price")
        if last_at is not None:
            age = datetime.now(timezone.utc) - last_at
            if age < timedelta(hours=cooldown_h) and last_price is not None and new_price >= int(last_price):
                return False, "cooldown"

    return True, reason


def _slug(text: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in text)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-")[:48] or "product"


def add_url(state: dict[str, Any], url: str, target_price: int | None) -> dict[str, Any]:
    site = site_from_url(url)
    parsed = urlparse(url)
    pid = _slug(f"{site}-{parsed.path.rstrip('/').split('/')[-1]}")
    if find_product(state, pid):
        pid = f"{pid}-{len(state['products']) + 1}"
    product = {
        "id": pid,
        "name": pid,
        "site": site,
        "url": url,
        "enabled": True,
        "target_price": target_price if target_price is not None else _env_int("DEFAULT_TARGET_PRICE", 155000),
        "match_keywords": [],
        "specs": {},
        "last_price": None,
        "lowest_seen": None,
        "last_checked_at": None,
        "in_stock": None,
        "consecutive_failures": 0,
        "last_alert": None,
        "history": [],
    }
    state["products"].append(product)
    return product


def dump_debug(product_id: str, html: str) -> Path:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = DEBUG_DIR / f"{product_id}.html"
    path.write_text(html, encoding="utf-8")
    return path


def process_product(
    product: dict[str, Any],
    *,
    dry_run: bool,
    debug_dump: bool,
    delay: bool,
) -> str:
    if delay:
        jitter_delay()

    url = product["url"]
    site = product.get("site") or site_from_url(url)
    fetch = fetch_page(url, site)

    if debug_dump and fetch.html:
        dump_debug(product["id"], fetch.html)

    if fetch.blocked or not fetch.html:
        if fetch.html:
            dump_debug(product["id"], fetch.html)
        record_failure(product)
        log.error(
            "%s fetch failed via %s (%s status=%s)",
            product["id"],
            fetch.backend,
            fetch.reason,
            fetch.status,
        )
        maybe_health_alert(product, dry_run, fetch.reason or "blocked")
        return "fail"

    extracted = extract_product(fetch.html, site, product.get("match_keywords") or [], extra_text=url)
    if extracted.price is None:
        if extracted.in_stock is False:
            log.info("%s is out of stock (title=%r)", product["id"], extracted.title)
            record_out_of_stock(product)
            return "ok"
        dump_debug(product["id"], fetch.html)
        record_failure(product)
        log.error("%s extract failed (source=%s title=%r)", product["id"], extracted.source, extracted.title)
        maybe_health_alert(product, dry_run, "price_not_found")
        return "fail"

    if extracted.keyword_mismatch:
        log.warning(
            "%s keyword drift: missing %s in title %r",
            product["id"],
            extracted.keyword_mismatch,
            extracted.title,
        )

    old_price = product.get("last_price")
    was_in_stock = product.get("in_stock")
    fire, reason = should_alert(product, extracted.price, extracted.in_stock, was_in_stock=was_in_stock)

    drop_abs = None
    drop_pct = None
    if old_price is not None:
        drop_abs = int(old_price) - extracted.price
        if int(old_price):
            drop_pct = (drop_abs / int(old_price)) * 100

    record_success(
        product,
        price=extracted.price,
        in_stock=extracted.in_stock,
    )
    if extracted.title and product.get("name") == product.get("id"):
        product["name"] = extracted.title[:120]

    lowest = product.get("lowest_seen") or {}
    log.info(
        "%s %s → %s (source=%s backend=%s stock=%s)",
        product["id"],
        old_price,
        extracted.price,
        extracted.source,
        fetch.backend,
        extracted.in_stock,
    )

    if fire:
        notify(
            "drop",
            {
                "name": product.get("name"),
                "url": url,
                "old_price": old_price,
                "new_price": extracted.price,
                "target_price": product.get("target_price"),
                "lowest_seen": lowest.get("price"),
                "reason": reason,
                "drop_abs": drop_abs if drop_abs and drop_abs > 0 else None,
                "drop_pct": drop_pct if drop_abs and drop_abs > 0 else None,
            },
            dry_run=dry_run,
        )
        record_alert(product, price=extracted.price, reason=reason)
        return "alert"
    return "ok"


def maybe_health_alert(product: dict[str, Any], dry_run: bool, reason: str) -> None:
    threshold = _env_int("FAILURE_ALERT_THRESHOLD", 3)
    failures = int(product.get("consecutive_failures") or 0)
    if failures < threshold:
        return
    last = product.get("last_alert") or {}
    if isinstance(last, dict) and last.get("reason") == "health" and failures > threshold:
        return
    notify(
        "health",
        {
            "name": product.get("name"),
            "url": product.get("url"),
            "consecutive_failures": failures,
            "reason": reason,
        },
        dry_run=dry_run,
    )
    record_alert(product, price=int(product.get("last_price") or 0), reason="health")


def run(args: argparse.Namespace) -> int:
    state = load_state(args.state)
    if args.add_url:
        product = add_url(state, args.add_url, args.target_price)
        save_state(state, args.state)
        log.info("added product %s", product["id"])
        if not args.check_added:
            return 0

    products = [p for p in state["products"] if p.get("enabled", True)]
    if args.only:
        wanted = set(args.only)
        products = [p for p in products if p["id"] in wanted]
        missing = wanted - {p["id"] for p in products}
        if missing:
            log.error("unknown --only ids: %s", ", ".join(sorted(missing)))
            return 2
    if not products:
        log.error("no enabled products to check")
        return 2

    results: list[str] = []
    for i, product in enumerate(products):
        try:
            status = process_product(
                product,
                dry_run=args.dry_run,
                debug_dump=args.debug_dump,
                delay=i > 0,
            )
        except Exception:
            log.exception("unhandled error on %s", product.get("id"))
            record_failure(product)
            maybe_health_alert(product, args.dry_run, "unhandled_exception")
            status = "fail"
        results.append(status)

    if not args.dry_run:
        save_state(state, args.state)
    else:
        log.info("dry-run: state file not written")

    fails = results.count("fail")
    log.info("done: %s ok/alert, %s failed of %s", len(results) - fails, fails, len(results))
    if fails == len(results):
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Laptop price-drop tracker")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH, help="Path to tracker_state.json")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and evaluate but do not write state or send alerts")
    parser.add_argument("--only", nargs="+", metavar="ID", help="Only check these product ids")
    parser.add_argument("--debug-dump", action="store_true", help="Write fetched HTML to debug/<id>.html")
    parser.add_argument("--add-url", metavar="URL", help="Append a product URL to the state file")
    parser.add_argument("--target-price", type=int, default=None, help="Target price used with --add-url")
    parser.add_argument(
        "--check-added",
        action="store_true",
        help="Also scrape a URL just added with --add-url",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    _load_dotenv(Path(__file__).resolve().parent / ".env")
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("curl_cffi impersonation: %s", _curl_cffi_status())
    return run(args)


def _curl_cffi_status() -> str:
    try:
        import curl_cffi  # noqa: F401

        return "enabled"
    except ImportError:
        return "not installed"


if __name__ == "__main__":
    sys.exit(main())
