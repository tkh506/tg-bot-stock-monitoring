# Changelog

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
