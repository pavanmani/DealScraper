"""Telegram Bot API and Discord webhook alerts. Secrets come from the environment."""

from __future__ import annotations

import html
import logging
import os
from typing import Any

import requests

log = logging.getLogger("dealscraper.notifier")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def configured_channels() -> list[str]:
    raw = os.environ.get("NOTIFY_CHANNELS", "telegram,discord")
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def format_inr(value: int | None) -> str:
    if value is None:
        return "n/a"
    s = str(int(value))
    if len(s) <= 3:
        grouped = s
    else:
        last3 = s[-3:]
        rest = s[:-3]
        parts: list[str] = []
        while rest:
            parts.append(rest[-2:])
            rest = rest[:-2]
        grouped = ",".join(reversed(parts)) + "," + last3
    return f"₹{grouped}"


def build_drop_message(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    name = payload.get("name") or "Unknown product"
    url = payload.get("url") or ""
    old = payload.get("old_price")
    new = payload.get("new_price")
    target = payload.get("target_price")
    lowest = payload.get("lowest_seen")
    reason = payload.get("reason") or "price_drop"
    drop_abs = payload.get("drop_abs")
    drop_pct = payload.get("drop_pct")

    drop_line = ""
    if drop_abs is not None:
        pct = f" ({drop_pct:.1f}%)" if drop_pct is not None else ""
        drop_line = f"Drop: {format_inr(drop_abs)}{pct}\n"

    html_msg = (
        f"<b>Price alert · {html.escape(name)}</b>\n"
        f"{html.escape(reason.replace('_', ' '))}\n\n"
        f"{format_inr(old)} → <b>{format_inr(new)}</b>\n"
        f"{drop_line}"
        f"Target: {format_inr(target)}\n"
        f"All-time low: {format_inr(lowest)}\n"
        f'<a href="{html.escape(url)}">Open listing</a>'
    )
    embed = {
        "title": f"Price alert · {name}",
        "url": url,
        "description": reason.replace("_", " "),
        "color": 5763719,
        "fields": [
            {"name": "Previous", "value": format_inr(old), "inline": True},
            {"name": "Now", "value": format_inr(new), "inline": True},
            {"name": "Target", "value": format_inr(target), "inline": True},
            {"name": "All-time low", "value": format_inr(lowest), "inline": True},
        ],
    }
    if drop_abs is not None:
        pct = f" ({drop_pct:.1f}%)" if drop_pct is not None else ""
        embed["fields"].insert(2, {"name": "Drop", "value": f"{format_inr(drop_abs)}{pct}", "inline": True})
    return html_msg, embed


def build_health_message(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    name = payload.get("name") or "Unknown product"
    url = payload.get("url") or ""
    failures = payload.get("consecutive_failures")
    reason = payload.get("reason") or "repeated fetch/extract failures"
    html_msg = (
        f"<b>Tracker health · {html.escape(name)}</b>\n"
        f"{html.escape(str(reason))}\n"
        f"Consecutive failures: {failures}\n"
        f'<a href="{html.escape(url)}">Open listing</a>\n'
        "Likely cause: site blocked the fetch, or a selector/JSON path rotated."
    )
    embed = {
        "title": f"Tracker health · {name}",
        "url": url,
        "description": str(reason),
        "color": 15158332,
        "fields": [
            {"name": "Consecutive failures", "value": str(failures), "inline": True},
        ],
    }
    return html_msg, embed


def send_telegram(html_msg: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log.warning("Telegram skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        return False
    resp = requests.post(
        TELEGRAM_API.format(token=token),
        json={
            "chat_id": chat_id,
            "text": html_msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=20,
    )
    if resp.status_code >= 400:
        log.warning("Telegram send failed: %s %s", resp.status_code, resp.text[:300])
        return False
    return True


def send_discord(embed: dict[str, Any], content: str | None = None) -> bool:
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        log.warning("Discord skipped: DISCORD_WEBHOOK_URL not set")
        return False
    body: dict[str, Any] = {"embeds": [embed]}
    if content:
        body["content"] = content
    resp = requests.post(webhook, json=body, timeout=20)
    if resp.status_code >= 400:
        log.warning("Discord send failed: %s %s", resp.status_code, resp.text[:300])
        return False
    return True


def notify(kind: str, payload: dict[str, Any], *, dry_run: bool = False) -> None:
    if kind == "health":
        html_msg, embed = build_health_message(payload)
    else:
        html_msg, embed = build_drop_message(payload)

    if dry_run:
        log.info("[dry-run] %s notification:\n%s", kind, html_msg)
        return

    channels = configured_channels()
    if not channels:
        log.warning("NOTIFY_CHANNELS is empty; nothing sent")
        return
    if "telegram" in channels:
        send_telegram(html_msg)
    if "discord" in channels:
        send_discord(embed)
