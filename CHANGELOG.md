# Changelog

---

## 2026-03-02 (session 7)

### Change 1 — Volume alert: switched to yfinance 10-day average (`robo_monitor_multistock.py`)

The volume alert baseline was previously self-computed from the bot's local 30-day JSON price history (7-day rolling average of completed trading days). This has been replaced with the native yfinance 10-day average daily volume (`averageDailyVolume10Day` / `averageVolume10days` from `.info`), which is more standard and eliminates dependency on locally accumulated history.

- New method `MultiStockMonitor.get_avg_volume_10d(ticker)` calls `yf.Ticker(ticker).info`; falls back to the existing 7-day local calculation if yfinance returns `None`.
- All pro-rata (intraday) adjustment logic is unchanged — it still scales the average by elapsed trading hours.
- Volume alert description updated: "vs 7-day avg" → "vs 10-day avg" across the bot help text.
- Alert type description in AI advisor prompts updated accordingly.

### Change 2 — Volume alert: skip on weekends (`robo_monitor_multistock.py`)

Previously, the volume comparison ran on weekends using the last trading day's stale full-day volume, producing false alerts. A new `is_market_weekend(ticker)` helper checks the current day-of-week in the stock's market timezone; if Saturday or Sunday, `vol_ratio` is set to `None` and the alert is skipped.

- Applied before the grace period and pro-rata checks — weekend takes priority.
- Public holidays are not handled (no external calendar library; weekends cover the main reported issue).

### Change 3 — New news and sentiment APIs in AI Advisors (`ai_advisor.py`, `pre_ipo_advisor.py`, `telegram_bot_multistock.py`, `config.json.example`)

Three new external APIs added to both `AIAdvisor` and `PreIpoAdvisor` (same additions, mirrored independently to keep `pre_ipo_advisor.py` self-contained):

| API | Purpose | Key field |
|-----|---------|-----------|
| Marketaux (`api.marketaux.com`) | News articles (preferred over yfinance when key present) | `config.json → marketaux.api_key` |
| Finnhub (`finnhub.io`) | Company news (supplemental) + fundamental metrics (P/E, EPS/revenue growth, margins, beta) | `config.json → finnhub.api_key` |
| Adanos Finance (`api.adanos.org`) | Reddit, X/Twitter, Polymarket sentiment (buzz score, bullish/bearish %, trend); replaces public Reddit scrape when key present | `config.json → adanos.api_key` |

All three keys are optional — each fails silently; the bot falls back to existing sources (yfinance news, public Reddit scrape) when keys are absent.

**New methods added to both classes:**
- `_get_marketaux_news(ticker)` → `list`
- `_get_finnhub_news(ticker)` → `list`
- `_get_finnhub_metrics(ticker)` → `dict`
- `_get_adanos_sentiment(ticker)` → `dict` with `reddit`, `x`, `polymarket` sub-keys

**Prompt changes:**
- News section now prefers Marketaux with yfinance as fallback; Finnhub news shown as a separate supplemental block
- Retail sentiment section: Adanos Reddit replaces public Reddit scrape; X/Twitter and Polymarket added as new sub-sections
- New "FUNDAMENTAL METRICS (Finnhub)" section inserted before the monthly price summary

**`telegram_bot_multistock.py`:** loads `marketaux_key`, `finnhub_key`, `adanos_key` from `config.json` and passes them to both advisors at startup.

---

## 2026-02-25 (session 6 — hotfix)

### Hotfix — Raise `max_tokens` in IPO Listing Advisor (`pre_ipo_advisor.py`)

**Problem:** Users received `"AI response was cut off (token limit reached). Please try again."` when running the IPO Listing Advisor for any stock. The `finish_reason == "length"` guard in `parse_and_validate()` was triggering because the AI response was being truncated.

**Root cause:** `max_tokens` was set to `4000` in `call_openrouter()`. The DXYZ prompt is substantially larger than a typical single-stock prompt (~240 trading days of full OHLCV + target stock data), and the AI response for this richer context requires more output tokens than the existing advisor.

**Fix:** Raised `max_tokens` from `4000` → `8000` in `pre_ipo_advisor.py`'s `call_openrouter()` method (line 930). Single-line change.

---

## 2026-02-25 (session 6)

### New Feature — IPO Listing Advisor (`pre_ipo_advisor.py`, `telegram_bot_multistock.py`)

Added a second, specialised AI advisor for analysing a pre-IPO / newly-listed stock by comparing it against DXYZ (Destiny Tech100) post-listing price behaviour.

**Architecture:**
- **New standalone file `pre_ipo_advisor.py`** — `PreIpoAdvisor` class. Intentionally self-contained and isolated from `ai_advisor.py` so it can be deleted without affecting the existing advisor when no longer relevant.
- **9 targeted edits to `telegram_bot_multistock.py`** — new import, global, conversation states (50–52), menu button, four handlers, ConversationHandler registration, `button_callback` branch, `setup_commands` entry, and `main()` initialisation.

**Key differences from existing AI Advisor:**
| | Ask AI Advice | IPO Listing Advisor |
|---|---|---|
| Primary data | Target stock 1y OHLCV | DXYZ full post-listing OHLCV (period="max") as primary reference |
| Signal selection | Buy / Sell | Buy / Sell |
| Main analysis focus | General price/sector/sentiment | DXYZ lifecycle phases → target lifecycle stage |
| Position sizing | Not included | SELL signal requires % to sell now + staged exit strategy |
| Conversation states | 40–42 | 50–52 |
| Entry callback | `ask_ai` / `/askai` | `ipo_advisor` / `/ipoai` |

**DXYZ reference data fetched per analysis:**
- Full OHLCV since listing via `yf.Ticker("DXYZ").history(period="max")` (~240 trading days)
- Listing date, open/close price, ATH (price, date, days to ATH), drawdown from ATH
- Price at Day 7 / 30 / 60 / 90 / 180 / 365 checkpoints (using `timedelta` forward-search)
- Volume: listing day, 10-day post-listing average, current 30d average
- Monthly summary + full daily OHLCV (all sent to AI as primary analytical framework)

**AI response schema additions:**
- `dxyz_pattern_analysis` (📊) — replaces `price_analysis` and `sector_analysis`; AI identifies DXYZ lifecycle phases with actual dates and price levels
- `lifecycle_stage` (📍) — where target stock sits on the DXYZ lifecycle curve
- `position_sizing` object — required for SELL signal, optional for BUY:
  - `sell_pct_now`: integer 0–100 (clamped and validated)
  - `rationale`: DXYZ-anchored justification
  - `staged_approach`: when/how to exit the remainder (ties to action-tier alert thresholds)

**Validation:** `parse_and_validate()` enforces `position_sizing` presence and structure for SELL signals; clamps `sell_pct_now` to 0–100.

**Telegram display order (SELL):** Summary → 📊 DXYZ Pattern → 📍 Lifecycle Stage → 💰 Position Sizing → 📰 News → 💬 Sentiment → 🌐 Macro → 🔔 Proposed Alerts.

**Callback data namespacing:** `ipo_advisor`, `ipostock_`, `iposignal_`, `ipoconfirm_` — no collisions with existing `ask_ai`, `aistock_`, `aisignal_`, `aiconfirm_`.

---

## 2026-02-25 (session 5)

### Cleanup — Reddit API (PRAW) Fully Removed (`ai_advisor.py`, `telegram_bot_multistock.py`, `config.json.example`)

Reddit official API (PRAW) was added and removed within the same session after Reddit's API policy changes made credentials harder to obtain. All references cleaned up across all three files — `reddit_cfg`, `AIAdvisor(reddit_config=...)`, and the `"reddit"` block in `config.json.example`.

---

### New Feature — Reddit Sentiment via Public `.json` Endpoint (`ai_advisor.py`)

Added `_get_reddit_sentiment()` — fetches ticker-specific posts from `r/investing`, `r/stocks`, and `r/wallstreetbets` using Reddit's public `.json` URL trick (no API key needed). Returns top 10 posts by score with title, score, upvote ratio, and comment count. HK tickers (`.HK` suffix) are skipped automatically — Reddit coverage is too sparse to be useful. Each subreddit request has a 5-second timeout; the method fails silently if Reddit rate-limits the VM.

---

### Fix — AI Response Truncation: `max_tokens` Raised to 4000 (`ai_advisor.py`)

`max_tokens` was 1500. GPT-5 mini (and other verbose models) were hitting this limit mid-JSON, producing `Unterminated string` parse errors. Raised to 4000 to give all models sufficient headroom for a full 6-alert response with detailed analysis sections.

---

### Fix — Truncation Guard: `finish_reason == "length"` Check (`ai_advisor.py`)

Added a check in `parse_and_validate()` that inspects `finish_reason` from the API response before attempting JSON parsing. If `finish_reason == "length"`, raises a clear `"AI response was cut off (token limit reached)"` error instead of a cryptic JSON parse failure. Partial content is logged to terminal for debugging.

---

### Improvement — AI Response Restructured into 5 Analysis Sections (`ai_advisor.py`, `telegram_bot_multistock.py`)

Replaced the single `reasoning` field in the AI JSON response with five explicit sections, forcing the AI to actively cover each data category:

| Field | Content |
|-------|---------|
| `price_analysis` | Price movement, volume, SMAs, 52-week range, momentum |
| `sector_analysis` | Sector/industry context, ETF composition, peer comparison |
| `news_analysis` | Most relevant recent news headlines and their likely impact |
| `sentiment_analysis` | StockTwits ratio, Reddit tone, Google Trends level and direction |
| `macro_analysis` | Economy, rates, geopolitics, government policy, currency |

Telegram message updated with matching emoji section headers (📈 📰 💬 🌐 🏭) for both `immediate` and `alerts` response types.

---

## 2026-02-25 (session 4)

### Bug Fix — AI Advisor: Openrouter `response_format` Incompatibility (`ai_advisor.py`)

**Problem:** `call_openrouter()` sent `"response_format": {"type": "json_object"}` in the API payload. This is an OpenAI-specific parameter; when sent to Claude models via Openrouter, it causes the `content` field in the response to be an empty string. `json.loads("")` then raises `Expecting value: line 1 column 1 (char 0)`.

**Fix:** Removed `response_format` from the payload entirely. Claude follows the JSON-only instruction in the prompt without needing the parameter.

---

### Bug Fix — AI Advisor: Trailing Text After JSON Causes Parse Failure (`ai_advisor.py`)

**Problem:** Claude occasionally appends a note or explanation after the closing `}` of the JSON object. `json.loads()` is strict and rejects any content after the JSON ends, raising `Extra data: line N column 1`.

**Fix:** Replaced `json.loads(stripped)` with `json.JSONDecoder().raw_decode(stripped)`, which parses the first complete JSON object and silently ignores anything that follows. Also added:
- A `stripped.find("{")` guard to skip any leading non-JSON text before the object
- An empty-content guard that logs the full raw API response and raises a clear error
- Markdown fence stripping (` ```json ... ``` `) in case the model wraps the JSON anyway
- Raw content logging on any parse failure to aid debugging

---

### UX Fix — AI Advisor: Analysis Message Preserved After Confirm/Decline (`telegram_bot_multistock.py`)

**Problem:** When the user tapped ✅ Yes or ❌ Cancel on the alert proposal, `ask_ai_confirm()` called `query.edit_message_text()` — overwriting the full AI analysis message with the confirmation result. Users lost the analysis immediately.

**Fix:** Replaced `edit_message_text()` with two steps:
1. `query.edit_message_reply_markup(reply_markup=None)` — removes the Yes/No buttons from the analysis message, leaving the analysis text intact
2. `query.message.reply_text()` — sends the confirmation (✅ Alerts Updated / ❌ Cancelled) as a fresh new message below

Both messages now remain permanently in the chat for the user's records.

---

### Change — AI Advisor: Alert Count Range Widened to 2–6 (`ai_advisor.py`)

Changed the prompt instruction from `"Provide 2 to 4 alerts total"` to `"Provide 2 to 6 alerts total"` to give the AI more flexibility. Validation code has no upper-limit enforcement, so the AI can now suggest more alerts for complex situations while still being guided to keep it concise.

---

### Updated — `MULTISTOCK_DEPLOY_GUIDE.md` Part 5

Rewrote Part 5 to enforce a dry-run-first workflow before any push or VM deployment. Added Step 5.1 (dry-run locally in PyCharm with test bot token), Step 5.2 (commit and push), Step 5.3 (deploy to VM). Separated steps that were previously one combined block.

---

## 2026-02-24 (session 3)

### Improvement — AI Advisor: Switch to yfinance 1-Year Data + Rich Technical Context

**Problem:** `prepare_context()` used only the local `price_history/` JSON (capped at 30 days, only as old as the stock was added to the bot). A freshly-added stock had almost no data for the AI to work with.

**Fix:** `ai_advisor.py` fully rewritten to fetch data directly from yfinance:

- **Primary data: 1 year of daily OHLCV** via `yf.Ticker(ticker).history(period="1y")` (~252 trading days)
- **Computed statistics** passed to AI:
  - 52-week high / low (with % distance from current price)
  - 20 / 50 / 200-day simple moving averages (with directional note)
  - Price changes: 1-day, 1-month, 3-month, 6-month, 1-year
  - Volume: latest session, 30-day average, ratio
  - Annualised 20-day volatility
- **Monthly summary** (end-of-month close for last 12 months) — gives AI the year-long trend in 12 data points
- **Recent 60-day daily OHLCV** — gives AI detailed short-term picture
- **Premium history** from local JSON (yfinance has no NAV data — local records remain the only source)
- **Fallback**: if yfinance fails, falls back to local 30-day history with a console warning
- **Prompt restructured**: replaced flat price list with structured sections (key stats, monthly summary, recent OHLCV, premium history if applicable)
- Added helper functions `_fmt_pct()`, `_fmt_price()`, `_ma_note()` for clean prompt formatting
- `telegram_bot_multistock.py` unchanged — signature of `get_advice()` preserved

---

## 2026-02-24 (session 2)

### New Feature — AI Advisor ("Ask AI Advice")

Added AI-powered stock analysis and alert suggestion via Claude Sonnet (Openrouter API).

**New file: `ai_advisor.py`**
- `AIAdvisor` class encapsulates the full analysis pipeline
- `prepare_context()` — gathers: 30-day local price history, yfinance news headlines (up to 5), market benchmark 30-day return (SPY for US, ^HSI for HK), yfinance stock info (sector, beta, PE, etc.)
- `build_prompt()` — structured prompt that defines all 4 bot alert types with exact semantics, provides all context data, and asks for JSON-only output in one of two schemas (`immediate` or `alerts`)
- `call_openrouter()` — POST to `https://openrouter.ai/api/v1/chat/completions` with model `anthropic/claude-sonnet-4-5`, `response_format: json_object`, 90s timeout
- `parse_and_validate()` — validates AI JSON response schema, checks all alert types/operators/tiers
- `get_advice()` — orchestrates the pipeline; always returns a dict with `ok: True/False`, never raises

**Modified: `telegram_bot_multistock.py`**
- New conversation states: `AI_SELECT_STOCK = 40`, `AI_SELECT_SIGNAL = 41`, `AI_CONFIRM_ALERTS = 42`
- New global `ai_advisor` instance (loaded at startup from `config.json`; `None` if key absent)
- New `UserConfigManager.replace_alerts()` — atomically replaces all alerts for a stock
- New `AlertStateManager.clear_stock_alert_states()` — purges stale alert state keys after alert replacement
- New handlers: `ask_ai_start`, `ask_ai_stock_selected`, `ask_ai_signal_selected`, `ask_ai_confirm`
- AI call uses `asyncio.to_thread()` — non-blocking; bot stays responsive during 20–40s AI call
- "🤖 Ask AI Advice" button added to main menu
- `ask_ai_conv` ConversationHandler registered before generic `button_callback`

**Modified: `config.json.example`**
- Added `openrouter.api_key` field

**UX flow:**
1. Main menu → 🤖 Ask AI Advice → select stock → 📈 Buy / 📉 Sell
2. Bot shows "Analysing... ⏳" immediately
3. AI result: immediate recommendation (no alert change) OR two-tier alert proposals
4. On confirmation: all alerts replaced + old alert states cleared; monitor picks up new alerts on next 15-min cycle

---

## 2026-02-24 (session 1)

### Bug Fix — Volume Alert: Phantom Entries on Non-Trading Days (`robo_monitor_multistock.py`)

**Problem:** The monitor used `datetime.now()` as the date key when saving price history. Since the monitor runs 24/7, weekend and holiday runs saved the last trading day's data under new calendar dates (e.g. Friday's data stored again under Saturday, Sunday, and Monday). This created phantom duplicate entries, which contaminated the 7-day volume average — e.g. after a 3-day weekend, the average could include 3 copies of the same Friday's volume.

**Fix:** `get_stock_data()` now returns the actual trading date from the yfinance DataFrame index (`latest.name`). `process_stock()` uses this `trade_date` instead of `datetime.now()`.

```python
# Before
today = datetime.now().strftime('%Y-%m-%d')
self.history.add_price_point(ticker, today, ...)

# After
price, volume, trade_date = self.get_stock_data(ticker)
self.history.add_price_point(ticker, trade_date, ...)
```

---

### Bug Fix — Volume Alert: Partial Intraday Volume in 7-Day Average (`robo_monitor_multistock.py`)

**Problem:** `get_volume_average_7d()` included today's entry (`daily_prices[-7:]`). During market hours, today's entry holds partial intraday volume, which skews the baseline average downward and inflates the computed `vol_ratio`.

**Fix:** The average is now computed from `daily_prices[:-1][-7:]` — the 7 most recent *completed* trading days, excluding today.

---

### New — GitHub Workflow

Replaced the manual file-paste deployment approach with a GitHub-based workflow.

**New files:**
- `.gitignore` — excludes `config.json`, data directories (`user_configs/`, `alert_states/`, `price_history/`), `__pycache__/`, `.DS_Store`
- `config.json.example` — committed template; actual `config.json` is created manually on the VM and never committed

**Update workflow (going forward):**
```bash
# Local: push changes
git add . && git commit -m "..." && git push

# VM: pull and restart
git pull
sudo systemctl restart robo-config-bot.service robo-monitor-bot.service
```

---

### New — `CLAUDE.md`

Created project reference file covering architecture, data formats, alert types, deployment workflow, and key design decisions. Intended for future AI-assisted sessions.

---

### Updated — `MULTISTOCK_DEPLOY_GUIDE.md`

Fully rewrote the deployment guide. Now covers:
- **Part 0:** Local testing with PyCharm (install deps, create local `config.json`, run both scripts, test via Telegram)
- **Part 1:** Push local code to GitHub (git init, PAT note)
- **Part 2:** Create Google Cloud free-tier VM (exact settings table)
- **Part 3:** VM initial setup (install deps, clone repo, create config)
- **Part 4:** Systemd service setup with full `.service` file templates
- **Part 5:** Future update workflow
- **Part 6:** Useful diagnostic commands
- **File Reference table:** what is/isn't in git

---

### Bug Fix — Double `stock_` Prefix in Callback Data (`telegram_bot_multistock.py`)

**Problem:** Stock IDs are generated as `stock_xxxxxxxx`. Four places in the bot were wrapping them again in `f"stock_{stock_id}"`, producing `stock_stock_xxxxxxxx` as callback data. `stock_detail()` then stripped all `stock_` occurrences, leaving just `xxxxxxxx` — which matched nothing in storage. Result: tapping any stock in the list silently failed.

**Fix:** Removed the redundant `stock_` prefix from the four callback builders. Updated `stock_detail()` to use `query.data` directly as the stock ID (no stripping needed).

Affected locations:
- `list_stocks()` — stock list buttons
- `stock_detail()` — ID extraction
- `manage_stock_alerts()` — Back button
- `update_nav_entered()` — Back button
- `remove_stock_confirm()` — Cancel button

---

### Bug Fix — Alerts Never Trigger on Fresh Install (`robo_monitor_multistock.py`)

**Problem:** On a fresh VM (empty `price_history/`), the monitor saves only today's price on the first run. `get_price_change_1d()` needs at least 2 entries and returns `None`. `check_alert()` returns `False` for `None` values. Result: all price change alerts silently never fire until the monitor has run across two separate trading days.

**Fix:** Added `_seed_history()` method. On first run for any ticker (fewer than 2 history entries), it fetches 15 days of data from yfinance and seeds the full history. Price change and volume alerts work from the very first monitor cycle.

---

### Bug Fix — Misleading Alert Description for Price Change Alerts (`telegram_bot_multistock.py`)

**Problem:** `_generate_description()` generated description based only on whether the threshold was positive or negative, ignoring the operator. So operator `>` with threshold `-2.5` produced "Drops >2.5% in 1 day" even though the actual condition was `change_1d > -2.5` (triggers almost always — not a drop alert).

**Fix:** Description now factors in both operator and threshold sign. Unusual combinations (e.g. `>` with negative threshold) show the explicit condition (`1D change > -2.5%`) instead of a misleading natural-language description.

---

### Fix — Python Output Not Appearing in Systemd Logs

**Problem:** Both bots run as systemd services. Python buffers `print()` output by default, so no log output appeared in `journalctl` — making debugging impossible.

**Fix:** Added `Environment="PYTHONUNBUFFERED=1"` to both `.service` file templates in the deploy guide. This forces Python to flush output immediately to the journal.
