# DealScraper — laptop price-drop tracker

Tracks a watchlist of Amazon.in / Flipkart product pages twice a day, compares live prices against your target and last seen values, and sends a Telegram or Discord alert on a real drop. Runs on GitHub Actions at zero cost.

Watchlist only. Keyword discovery over search pages is a later phase.

## How it works

1. `scraper.py` loads [`data/tracker_state.json`](data/tracker_state.json).
2. Each enabled URL is fetched (direct first; scraping APIs only if the page looks blocked).
3. Price is extracted from JSON-LD / `__INITIAL_STATE__` / a small CSS fallback chain — not from rotating Flipkart class hashes.
4. An alert fires when the listing is in stock **and** the price is at/below `target_price`, **or** it dropped from `last_price` by at least `max(₹2000, 3%)`. A new all-time low always alerts. The same price sitting under target is suppressed for 24 hours.
5. `last_price` is always updated (so a later increase does not freeze alerts at the old low). `lowest_seen` is tracked separately.
6. GitHub Actions commits the updated JSON back to the repo.

## Local setup

Python 3.12+ (CI uses 3.12). Optional `curl_cffi` improves TLS fingerprints against Amazon WAF if you have a wheel for your platform.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env   # Windows
# cp .env.example .env   # macOS/Linux
```

Fill `.env` only if you want live notifications or a scraping-API fallback. Then:

```bash
python scraper.py --dry-run -v
python scraper.py --only hp-omen-16-an0015tx-amazon --debug-dump
python scraper.py --add-url "https://www.amazon.in/dp/B0XXXXXXXX" --target-price 155000
```

`--dry-run` fetches and evaluates but does not write `tracker_state.json` or send alerts.

## GitHub secrets and variables

Repo → **Settings → Secrets and variables → Actions**.

### Secrets (required for alerts and optional fetch fallback)

| Name | What it is |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) (`/newbot`) |
| `TELEGRAM_CHAT_ID` | Your chat or group id |
| `DISCORD_WEBHOOK_URL` | Channel → Edit channel → Integrations → Webhooks |
| `SCRAPERAPI_KEY` | Optional. Used only when a direct fetch is blocked |
| `ZENROWS_API_KEY` | Optional. Same as above |

Telegram chat id: send any message to your bot, then open

`https://api.telegram.org/bot<TOKEN>/getUpdates`

and copy `result[0].message.chat.id`. For a group, add the bot and mention it once.

You can use Telegram only, Discord only, or both. Missing credentials log a warning; the run still updates prices.

### Variables (optional)

| Name | Default | Purpose |
| --- | --- | --- |
| `RUNNER_LABEL` | `ubuntu-latest` | Set to `self-hosted` to run on your own PC (residential IP, no API quota) |
| `FETCH_BACKENDS` | `direct,scraperapi,zenrows` | Backend order |
| `NOTIFY_CHANNELS` | `telegram,discord` | Which channels to use |
| `MIN_DROP_ABS` | `2000` | Minimum ₹ drop to alert when above target |
| `MIN_DROP_PCT` | `0.03` | Minimum % drop (whichever is larger with `MIN_DROP_ABS`) |
| `ALERT_COOLDOWN_HOURS` | `24` | Suppress repeat alerts at the same or higher price |
| `FAILURE_ALERT_THRESHOLD` | `3` | Health ping after this many consecutive failures |

After pushing, run **Actions → Price tracker → Run workflow** once to confirm secrets. The schedule is `0 3,15 * * *` (08:30 and 20:30 IST).

## Adding products

Edit `data/tracker_state.json` (or `--add-url`):

```json
{
  "id": "unique-slug",
  "name": "Display name",
  "site": "amazon",
  "url": "https://www.amazon.in/dp/B0........",
  "enabled": true,
  "target_price": 155000,
  "match_keywords": ["Core Ultra 7", "RTX 5060"]
}
```

`match_keywords` are checked against the fetched title. A mismatch is logged (variant/ASIN drift) and does **not** fire a drop alert by itself. Set `enabled: false` to skip a row without deleting history.

Prefer canonical product URLs (`/dp/ASIN` on Amazon, `/p/itm…` on Flipkart). Search pages are not scraped.

## Fetch strategy

GitHub-hosted runners are Azure datacenter IPs. Amazon.in often returns 503 / CAPTCHA / a silent 200 CAPTCHA page from those ranges before User-Agent is even read. This tracker:

1. Tries a direct `requests` GET with a full, self-consistent browser header set.
2. Treats the page as blocked if status is 403/429/503, a captcha marker is present, **or** the site sentinel is missing (`#productTitle` / `__INITIAL_STATE__`). That last check catches silent 200 CAPTCHA pages.
3. Escalates to ScraperAPI then ZenRows **only** if a key is set.

Free-tier budget for 3 products × 2 runs/day = 180 requests/month:

- ScraperAPI free: 1,000 credits/month; Amazon costs 5 credits → ~900 if every request hits the API.
- ZenRows free: 5,000 credits/month; protected Amazon is 25 credits → ~4,500.

Both fit; neither has headroom for a large watchlist. Prefer a **self-hosted runner** on this machine (set repo variable `RUNNER_LABEL=self-hosted`) if you want zero API usage.

## Selector maintenance

Amazon prices come from `#corePriceDisplay_desktop_feature_div .a-price-whole` (and fallbacks). Flipkart CSS class names rotate (`_30jeq3`, `Nx9bqj`, … are already dead). Extraction prefers:

1. `application/ld+json` → `offers.price`
2. `window.__INITIAL_STATE__` → first `finalPrice` (PDP product, not the recommendation carousel)
3. A buy-box-scoped ₹ regex, never the whole page

If a product fails three runs in a row you get one health alert. Re-run with `--debug-dump` and inspect `debug/<id>.html`, or download the `debug-html` Actions artifact (kept 3 days, uploaded on failed jobs).

Stock is read from Amazon `#availability` only — a page-wide “out of stock” search false-positives on in-stock listings.

## Notes

- Keep the watchlist small and the cadence twice daily. Aggressive scraping can violate site terms of service and will get datacenter IPs banned faster.
- GitHub disables scheduled workflows after 60 days of repository inactivity. The state auto-commit counts as activity, so the schedule stays alive while the tracker is actually updating prices.
